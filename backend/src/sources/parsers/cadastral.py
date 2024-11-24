from pathlib import Path
import sys
import asyncio
import os
import xml.etree.ElementTree as ET
from tqdm import tqdm
import geopandas as gpd
import aiohttp
import pandas as pd
from dotenv import load_dotenv
from shapely.geometry import Polygon, MultiPolygon
import asyncpg
import logging
import time
from collections import deque
from asyncio import Queue

from ...base import Source, clean_value

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Cadastral(Source):
    def __init__(self, config):
        super().__init__(config)
        load_dotenv()
        self.username = os.getenv('DATAFORDELER_USERNAME')
        self.password = os.getenv('DATAFORDELER_PASSWORD')
        if not self.username or not self.password:
            raise ValueError("Missing DATAFORDELER_USERNAME or DATAFORDELER_PASSWORD environment variables")
        # Keep original values for these parameters
        self.page_size = 10000
        self.max_concurrent = 10
        self.batch_size = 1000
        self.namespaces = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'mat': 'http://data.gov.dk/schemas/matrikel/1',
            'gml': 'http://www.opengis.net/gml/3.2'
        }

    def _get_session(self):
        """Create an HTTP session with connection pooling"""
        connector = aiohttp.TCPConnector(limit=self.max_concurrent)
        return aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0 QGIS/33603/macOS 15.1'},
            connector=connector
        )

    def _get_params(self, start_index=0, max_features=None):
        """Get WFS request parameters"""
        params = {
            'username': self.username,
            'password': self.password,
            'SERVICE': 'WFS',
            'REQUEST': 'GetFeature',
            'VERSION': '1.1.0',
            'TYPENAME': 'mat:SamletFastEjendom_Gaeldende',
            'SRSNAME': 'EPSG:25832',
            'startIndex': str(start_index),
            'NAMESPACE': 'xmlns(mat=http://data.gov.dk/schemas/matrikel/1)'
        }
        if max_features:
            params['maxFeatures'] = str(max_features)
        return params

    def _parse_geometry(self, geom, namespaces):
        """Parse geometry from XML"""
        if geom.tag.endswith('Polygon'):
            coords = []
            for pos_list in geom.findall('.//gml:posList', namespaces):
                points = [float(x) for x in pos_list.text.split()]
                coords.append([[points[i], points[i+1]] for i in range(0, len(points), 2)])
            return {'type': 'Polygon', 'coordinates': coords}
            
        elif geom.tag.endswith('MultiSurface'):
            coords = []
            for polygon in geom.findall('.//gml:Polygon', namespaces):
                poly_coords = []
                for pos_list in polygon.findall('.//gml:posList', namespaces):
                    points = [float(x) for x in pos_list.text.split()]
                    poly_coords.append([[points[i], points[i+1]] for i in range(0, len(points), 2)])
                coords.append(poly_coords)
            return {'type': 'MultiPolygon', 'coordinates': coords}
            
        return None

    def _parse_feature(self, member, namespaces):
        """Parse a single feature from XML"""
        feature = {
            'type': 'Feature',
            'properties': {},
            'geometry': None
        }
        
        for element in member:
            if element.tag.endswith('geometri'):
                continue
                
            field_name = element.tag.split('}')[-1]
            
            if element.text and element.text.strip():
                value = element.text.strip()
                
                if value.lower() in ('true', 'false'):
                    value = value.lower() == 'true'
                elif value.isdigit():
                    value = int(value)
                elif value.replace('.', '').isdigit() and '.' in value:
                    value = float(value)
                    
                feature['properties'][field_name] = value
        
        geom_elem = member.find('mat:geometri', namespaces)
        if geom_elem is not None:
            multi_surface = geom_elem.find('.//gml:MultiSurface', namespaces)
            if multi_surface is not None:
                feature['geometry'] = self._parse_geometry(multi_surface, namespaces)
            else:
                polygon = geom_elem.find('.//gml:Polygon', namespaces)
                if polygon is not None:
                    feature['geometry'] = self._parse_geometry(polygon, namespaces)
            
        return feature

    async def _fetch_chunk(self, session, start_index):
        """Fetch and parse a chunk of features"""
        params = self._get_params(start_index, self.page_size)
        features = []
        
        logger.info(f"Fetching chunk starting at index {start_index}")
        try:
            async with session.get(self.config['url'], params=params) as response:
                response.raise_for_status()
                logger.info(f"Received response for chunk {start_index}")
                
                parser = ET.XMLPullParser(['end'])
                chunk_size = 8192
                
                while True:
                    chunk = await response.content.read(chunk_size)
                    if not chunk:
                        break
                    parser.feed(chunk)
                    for event, elem in parser.read_events():
                        if elem.tag.endswith('SamletFastEjendom_Gaeldende'):
                            feature = self._parse_feature(elem, self.namespaces)
                            features.append(feature)
                            elem.clear()
                
                logger.info(f"Processed {len(features)} features from chunk {start_index}")
                return features
        except Exception as e:
            logger.error(f"Error fetching chunk {start_index}: {str(e)}")
            raise
        finally:
            # Cleanup for Cloud Run
            if 'parser' in locals():
                parser.close()
                del parser

    async def _fetch_chunk_with_retry(self, session, start_index, max_retries=3, initial_delay=1):
        """Fetch chunk with exponential backoff retry"""
        delay = initial_delay
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                return await self._fetch_chunk(session, start_index)
            except Exception as e:
                last_exception = e
                if attempt < max_retries - 1:  # Don't sleep on the last attempt
                    logger.warning(f"Attempt {attempt + 1} failed for chunk {start_index}: {str(e)}. Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    logger.error(f"All {max_retries} attempts failed for chunk {start_index}")
        
        raise last_exception

    async def sync(self, client):
        """Sync cadastral data to PostgreSQL"""
        logger.info("Starting cadastral sync...")
        
        try:
            # Create database tables
            await self._create_tables(client)
            
            # Fetch and process data
            gdf = await self.fetch()
            
            # Prepare records
            records = self._prepare_records(gdf)
            
            # Sync to database
            total_synced = await self._batch_insert(client, records)
            
            logger.info(f"Successfully synced {total_synced:,} records")
            return total_synced
            
        except Exception as e:
            logger.error(f"Sync failed: {str(e)}")
            raise

    async def _create_tables(self, client):
        """Create necessary database tables"""
        await client.execute("""
            CREATE TABLE IF NOT EXISTS cadastral_properties (
                bfe_number INTEGER PRIMARY KEY,
                business_event TEXT,
                business_process TEXT,
                latest_case_id TEXT,
                id_namespace TEXT,
                id_local TEXT,
                registration_from TIMESTAMP WITH TIME ZONE,
                effect_from TIMESTAMP WITH TIME ZONE,
                authority TEXT,
                is_worker_housing BOOLEAN,
                is_common_lot BOOLEAN,
                has_owner_apartments BOOLEAN,
                is_separated_road BOOLEAN,
                agricultural_notation TEXT,
                geometry GEOMETRY(MULTIPOLYGON, 25832)
            );
            
            CREATE INDEX IF NOT EXISTS cadastral_properties_geometry_idx 
            ON cadastral_properties USING GIST (geometry);
        """)

    async def fetch(self) -> pd.DataFrame:
        """Fetch cadastral data using parallel streaming requests"""
        logger.info("Starting fetch process...")
        
        all_features = []
        
        async with self._get_session() as session:
            # Get total count
            logger.info("Getting total feature count...")
            count_params = self._get_params()
            count_params['resultType'] = 'hits'
            
            async with session.get(self.config['url'], params=count_params) as response:
                response.raise_for_status()
                content = await response.text()
                root = ET.fromstring(content)
                total_features = int(root.get('numberMatched', '0'))
                
                if total_features == 0:
                    raise ValueError("Could not determine total number of features")
                
            logger.info(f"Found {total_features:,} total features")
            
            # Process in batches
            batch_size = self.page_size * self.max_concurrent
            for batch_start in range(0, total_features, batch_size):
                logger.info(f"Processing batch starting at {batch_start:,}")
                tasks = []
                for offset in range(0, batch_size, self.page_size):
                    start_idx = batch_start + offset
                    if start_idx >= total_features:
                        break
                    tasks.append(self._fetch_chunk_with_retry(session, start_idx))
                
                logger.info(f"Fetching records {batch_start:,} to {min(batch_start + batch_size, total_features):,}...")
                batch_results = await asyncio.gather(*tasks)
                
                for features in batch_results:
                    all_features.extend(features)
                    logger.info(f"Total features collected: {len(all_features):,}")
        
        logger.info("Fetch process completed")
        logger.info(f"Converting {len(all_features):,} features to GeoDataFrame...")
        gdf = gpd.GeoDataFrame.from_features(all_features)
        gdf.set_crs(epsg=25832, inplace=True)
        return gdf

    def _prepare_records(self, gdf):
        """Convert GeoDataFrame rows to database records"""
        records = []
        for _, row in gdf.iterrows():
            record = {
                'bfe_number': clean_value(row['BFEnummer']),
                'business_event': clean_value(row['forretningshaendelse']),
                'business_process': clean_value(row['forretningsproces']),
                'latest_case_id': clean_value(row['senesteSagLokalId']),
                'id_namespace': clean_value(row['id.namespace']),
                'id_local': clean_value(row['id.lokalId']),
                'registration_from': clean_value(row['registreringFra']),
                'effect_from': clean_value(row['virkningFra']),
                'authority': clean_value(row['virkningsaktoer']),
                'is_worker_housing': clean_value(row['arbejderbolig']),
                'is_common_lot': clean_value(row['erFaelleslod']),
                'has_owner_apartments': clean_value(row['hovedejendomOpdeltIEjerlejligheder']),
                'is_separated_road': clean_value(row['udskiltVej']),
                'agricultural_notation': clean_value(row['landbrugsnotering']),
                'geometry': row['geometry'].wkt if 'geometry' in row else None
            }
            records.append(record)
        return records

    async def _batch_insert(self, client, records):
        """Insert records in batches"""
        batch_size = 2000
        total_records = len(records)
        total_synced = 0
        
        logger.info("Starting database upload...")
        for i in range(0, total_records, batch_size):
            batch = records[i:i + batch_size]
            query = """
                INSERT INTO cadastral_properties (
                    bfe_number, business_event, business_process, latest_case_id,
                    id_namespace, id_local, registration_from, effect_from,
                    authority, is_worker_housing, is_common_lot, has_owner_apartments,
                    is_separated_road, agricultural_notation, geometry
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, ST_GeomFromText($15, 25832))
                ON CONFLICT (bfe_number) DO UPDATE SET
                    business_event = EXCLUDED.business_event,
                    business_process = EXCLUDED.business_process,
                    latest_case_id = EXCLUDED.latest_case_id,
                    id_namespace = EXCLUDED.id_namespace,
                    id_local = EXCLUDED.id_local,
                    registration_from = EXCLUDED.registration_from,
                    effect_from = EXCLUDED.effect_from,
                    authority = EXCLUDED.authority,
                    is_worker_housing = EXCLUDED.is_worker_housing,
                    is_common_lot = EXCLUDED.is_common_lot,
                    has_owner_apartments = EXCLUDED.has_owner_apartments,
                    is_separated_road = EXCLUDED.is_separated_road,
                    agricultural_notation = EXCLUDED.agricultural_notation,
                    geometry = ST_GeomFromText(EXCLUDED.geometry, 25832)
            """
            await client.executemany(query, [
                (
                    r['bfe_number'], r['business_event'], r['business_process'],
                    r['latest_case_id'], r['id_namespace'], r['id_local'],
                    r['registration_from'], r['effect_from'], r['authority'],
                    r['is_worker_housing'], r['is_common_lot'], r['has_owner_apartments'],
                    r['is_separated_road'], r['agricultural_notation'], r['geometry']
                ) for r in batch
            ])
            total_synced += len(batch)
            if total_synced % 10000 == 0:
                logger.info(f"Synced {total_synced:,} of {total_records:,} records")
        
        return total_synced
