from pathlib import Path
import asyncio
import xml.etree.ElementTree as ET
import logging
import aiohttp
from shapely.geometry import Polygon
import backoff
from aiohttp import ClientError, ClientTimeout
from ...base import Source

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
            return Polygon(coords)
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

    @backoff.on_exception(
        backoff.expo,
        (ClientError, asyncio.TimeoutError),
        max_tries=3
    )
    async def _fetch_chunk(self, session, start_index):
        """Fetch a chunk of features with retries"""
        async with self.request_semaphore:
            params = self._get_params(start_index)
            async with session.get(self.config['url'], params=params) as response:
                response.raise_for_status()
                text = await response.text()
                root = ET.fromstring(text)
                
                features = []
                for feature_elem in root.findall('.//natur:kulstof2022', self.namespaces):
                    feature = self._parse_feature(feature_elem)
                    if feature:
                        features.append(feature)
                
                return features

    async def sync(self):
        """Sync wetlands data to Cloud Storage"""
        logger.info("Starting wetlands sync...")
        
        async with aiohttp.ClientSession() as session:
            # Get total count
            params = self._get_params(0)
            async with session.get(self.config['url'], params=params) as response:
                text = await response.text()
                root = ET.fromstring(text)
                total_features = int(root.get('numberMatched', '0'))
                logger.info(f"Total available features: {total_features:,}")
                
                # Process first batch
                features = [
                    self._parse_feature(f) 
                    for f in root.findall('.//natur:kulstof2022', self.namespaces)
                ]
                features = [f for f in features if f]
                
                if features:
                    await self.write_to_storage(features, 'wetlands')
                logger.info(f"Wrote first batch: {len(features)} features")
                
                # Process remaining batches
                total_processed = len(features)
                for start_index in range(self.batch_size, total_features, self.batch_size):
                    try:
                        chunk = await self._fetch_chunk(session, start_index)
                        if chunk:
                            await self.write_to_storage(chunk, 'wetlands')
                            total_processed += len(chunk)
                            logger.info(f"Progress: {total_processed:,}/{total_features:,}")
                    except Exception as e:
                        logger.error(f"Error processing batch at {start_index}: {str(e)}")
                        continue
        
        logger.info(f"Sync completed. Total processed: {total_processed:,}")
        return total_processed

    async def fetch(self):
        """Not implemented - using sync() directly"""
        raise NotImplementedError("This source uses sync() directly") 