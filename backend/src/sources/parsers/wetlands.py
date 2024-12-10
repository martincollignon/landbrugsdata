from pathlib import Path
import asyncio
import xml.etree.ElementTree as ET
import logging
import aiohttp
from shapely.geometry import Polygon, MultiPolygon
from aiohttp import ClientError
from ...base import Source
import pandas as pd
import geopandas as gpd
import os
from ..utils.geometry_validator import validate_and_transform_geometries
import time
import psutil
from collections import Counter
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

class Wetlands(Source):
    def __init__(self, config):
        super().__init__(config)
        self.batch_size = 100000
        self.max_concurrent = 5
        self.request_timeout = 300
        
        self.namespaces = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'natur': 'http://wfs2-miljoegis.mim.dk/natur',
            'gml': 'http://www.opengis.net/gml/3.2'
        }
        
        self.request_semaphore = asyncio.Semaphore(self.max_concurrent)

    def analyze_geometry(self, geom):
        """Analyze a single geometry for grid characteristics"""
        bounds = geom.bounds
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        area = width * height
        
        # Check grid alignment
        vertices = list(geom.exterior.coords)
        is_grid_aligned = all(
            abs(round(coord / 10) * 10 - coord) < 0.01
            for vertex in vertices
            for coord in vertex
        )
        
        return {
            'width': width,
            'height': height,
            'area': area,
            'grid_aligned': is_grid_aligned,
            'vertices': len(vertices)
        }

    def log_geometry_statistics(self, gdf):
        """Analyze and log statistics about the geometries"""
        stats = []
        for geom in gdf.geometry:
            stats.append(self.analyze_geometry(geom))
        
        # Convert to DataFrame for easy analysis
        stats_df = pd.DataFrame(stats)
        
        # Unique dimensions
        dimensions = Counter(zip(stats_df['width'], stats_df['height']))
        
        logger.info("Geometry Statistics:")
        logger.info(f"Total features: {len(stats_df)}")
        logger.info("\nUnique dimensions (width x height, count):")
        for (width, height), count in dimensions.most_common():
            logger.info(f"{width:.1f}m x {height:.1f}m: {count} features")
        
        logger.info(f"\nNon-grid-aligned features: {sum(~stats_df['grid_aligned'])}")
        logger.info(f"Average vertices per feature: {stats_df['vertices'].mean():.1f}")
        logger.info(f"Total area covered: {stats_df['area'].sum() / 1_000_000:.2f} km²")

    def _get_params(self, start_index=0):
        """Get WFS request parameters"""
        return {
            'SERVICE': 'WFS',
            'REQUEST': 'GetFeature',
            'VERSION': '2.0.0',
            'TYPENAMES': self.config['layer'],
            'SRSNAME': 'EPSG:25832',
            'count': str(self.batch_size),
            'startIndex': str(start_index)
        }

    def _parse_geometry(self, geom_elem):
        """Parse GML geometry into Shapely geometry"""
        try:
            coords = geom_elem.find('.//gml:posList', self.namespaces).text.split()
            coords = [(float(coords[i]), float(coords[i + 1])) 
                     for i in range(0, len(coords), 2)]
            poly = Polygon(coords)
            
            # Ensure the polygon is valid
            if not poly.is_valid:
                poly = poly.buffer(0)
            return poly
        except Exception as e:
            logger.error(f"Error parsing geometry: {str(e)}")
            return None

    def _parse_feature(self, feature):
        """Parse a single feature into GeoJSON-like dictionary"""
        try:
            geom = self._parse_geometry(
                feature.find('.//gml:Polygon', self.namespaces)
            )
            
            if not geom:
                return None

            return {
                'type': 'Feature',
                'geometry': geom.__geo_interface__,
                'properties': {
                    'id': feature.get('{http://www.opengis.net/gml/3.2}id'),
                    'gridcode': int(feature.find('natur:gridcode', self.namespaces).text),
                    'toerv_pct': feature.find('natur:toerv_pct', self.namespaces).text
                }
            }
        except Exception as e:
            logger.error(f"Error parsing feature: {str(e)}")
            return None

    async def sync(self):
        """Sync wetlands data to Cloud Storage"""
        logger.info("Starting wetlands sync...")
        self.is_sync_complete = False
        
        # Clean up any existing working files
        working_blob = self.bucket.blob('raw/wetlands/working.parquet')
        if working_blob.exists():
            working_blob.delete()
        
        async with aiohttp.ClientSession() as session:
            # Get total count
            params = self._get_params(0)
            async with session.get(self.config['url'], params=params) as response:
                text = await response.text()
                root = ET.fromstring(text)
                total_features = int(root.get('numberMatched', '0'))
                logger.info(f"Total available features: {total_features:,}")
                
                features = [
                    self._parse_feature(f) 
                    for f in root.findall('.//natur:kulstof2022', self.namespaces)
                ]
                features = [f for f in features if f]
                
                if features:
                    await self.write_to_storage(features, 'wetlands')
                logger.info(f"Wrote first batch: {len(features)} features")
                
                total_processed = len(features)
                for start_index in range(self.batch_size, total_features, self.batch_size):
                    try:
                        chunk = await self._fetch_chunk(session, start_index)
                        if chunk:
                            self.is_sync_complete = (start_index + self.batch_size) >= total_features
                            await self.write_to_storage(chunk, 'wetlands')
                            total_processed += len(chunk)
                            logger.info(f"Progress: {total_processed:,}/{total_features:,}")
                    except Exception as e:
                        logger.error(f"Error processing batch at {start_index}: {str(e)}")
                        continue
        
        logger.info(f"Sync completed. Total processed: {total_processed:,}")
        return total_processed

    async def write_to_storage(self, features, dataset):
        """Write features to GeoParquet in Cloud Storage"""
        if not features:
            return
        
        try:
            # Create DataFrame
            df = pd.DataFrame([f['properties'] for f in features])
            geometries = [Polygon(f['geometry']['coordinates'][0]) for f in features]
            
            # Create GeoDataFrame
            gdf = gpd.GeoDataFrame(df, geometry=geometries, crs="EPSG:25832")
            
            # Handle working/final files
            temp_working = f"/tmp/{dataset}_working.parquet"
            working_blob = self.bucket.blob(f'raw/{dataset}/working.parquet')
            
            if working_blob.exists():
                working_blob.download_to_filename(temp_working)
                existing_gdf = gpd.read_parquet(temp_working)
                logger.info(f"Appending {len(gdf):,} features to existing {len(existing_gdf):,}")
                combined_gdf = pd.concat([existing_gdf, gdf], ignore_index=True)
            else:
                combined_gdf = gdf
            
            # Write working file
            combined_gdf.to_parquet(temp_working)
            working_blob.upload_from_filename(temp_working)
            logger.info(f"Updated working file now has {len(combined_gdf):,} features")
            
            # If sync complete, create final files
            if hasattr(self, 'is_sync_complete') and self.is_sync_complete:
                logger.info(f"Starting merge of {len(combined_gdf):,} features...")
                start_time = time.time()
                
                # Create spatial index for efficient neighbor finding
                logger.info("Creating spatial index...")
                combined_gdf['idx'] = range(len(combined_gdf))
                spatial_index = combined_gdf.sindex
                
                # Function to check if two polygons share an edge
                def shares_edge(geom1, geom2):
                    intersection = geom1.intersection(geom2)
                    return (intersection.geom_type == 'LineString' and 
                           intersection.length >= 10)  # At least one grid cell length
                
                # Find and merge adjacent polygons
                logger.info("Finding and merging adjacent polygons...")
                merged = set()  # Keep track of merged polygons
                merged_polygons = []
                
                for idx, row in combined_gdf.iterrows():
                    if idx in merged:
                        continue
                    
                    # Find potential neighbors using spatial index
                    bounds = row.geometry.bounds
                    possible_matches_idx = list(spatial_index.intersection(bounds))
                    possible_matches = combined_gdf.iloc[possible_matches_idx]
                    
                    # Start with current polygon
                    current_group = [row.geometry]
                    merged.add(idx)
                    
                    # Check each potential neighbor
                    for match_idx, match_row in possible_matches.iterrows():
                        if match_idx != idx and match_idx not in merged:
                            if shares_edge(row.geometry, match_row.geometry):
                                current_group.append(match_row.geometry)
                                merged.add(match_idx)
                    
                    # Merge the group if we found any adjacent polygons
                    if len(current_group) > 1:
                        merged_poly = unary_union(current_group)
                    else:
                        merged_poly = current_group[0]
                    
                    merged_polygons.append(merged_poly)
                    
                    if len(merged_polygons) % 10000 == 0:
                        logger.info(f"Processed {len(merged_polygons):,} groups")
                
                # Create new GeoDataFrame with merged polygons
                dissolved_gdf = gpd.GeoDataFrame(
                    geometry=merged_polygons,
                    crs=combined_gdf.crs
                )
                
                # Add wetland_id
                dissolved_gdf['wetland_id'] = range(1, len(dissolved_gdf) + 1)
                
                logger.info(f"Created {len(dissolved_gdf):,} merged polygons")
                logger.info(f"Reduced from {len(combined_gdf):,} grid cells")
                logger.info(f"Processing took {time.time() - start_time:.2f} seconds")
                
                # Log statistics for the dissolved geometries
                logger.info("\nAnalyzing dissolved geometries:")
                self.log_geometry_statistics(dissolved_gdf)
                
                # Transform and validate final geometries
                logger.info("Transforming geometries to BigQuery-compatible CRS...")
                dissolved_gdf = validate_and_transform_geometries(dissolved_gdf, 'wetlands')
                
                # Write dissolved version
                temp_dissolved = f"/tmp/{dataset}_dissolved.parquet"
                dissolved_gdf.to_parquet(temp_dissolved)
                dissolved_blob = self.bucket.blob(f'raw/{dataset}/dissolved_current.parquet')
                dissolved_blob.upload_from_filename(temp_dissolved)
                
                # Cleanup
                working_blob.delete()
                os.remove(temp_dissolved)
            
            # Cleanup working file
            if os.path.exists(temp_working):
                os.remove(temp_working)
            
        except Exception as e:
            logger.error(f"Error writing to storage: {str(e)}")
            raise

    async def fetch(self):
        """Not implemented - using sync() directly"""
        raise NotImplementedError("This source uses sync() directly")

    async def _fetch_chunk(self, session, start_index):
        """Fetch a chunk of features starting at the given index"""
        try:
            async with self.request_semaphore:
                params = self._get_params(start_index)
                async with session.get(
                    self.config['url'], 
                    params=params, 
                    timeout=self.request_timeout
                ) as response:
                    text = await response.text()
                    root = ET.fromstring(text)
                    
                    features = [
                        self._parse_feature(f) 
                        for f in root.findall('.//natur:kulstof2022', self.namespaces)
                    ]
                    return [f for f in features if f]
                    
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching batch at {start_index}")
            return None
        except Exception as e:
            logger.error(f"Error fetching batch at {start_index}: {str(e)}")
            return None