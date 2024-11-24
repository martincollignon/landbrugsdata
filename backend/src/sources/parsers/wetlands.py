from pathlib import Path
import asyncio
import xml.etree.ElementTree as ET
import geopandas as gpd
import aiohttp
import logging
from shapely.geometry import Polygon
from ...base import Source, clean_value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Wetlands(Source):
    def __init__(self, config):
        super().__init__(config)
        self.batch_size = 100000  # WFS server seems happy with this batch size
        self.max_concurrent = 5   # Limit concurrent requests to be nice to the server
        self.namespaces = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'natur': 'http://wfs2-miljoegis.mim.dk/natur',
            'gml': 'http://www.opengis.net/gml/3.2'
        }

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
        """Parse GML geometry into WKT"""
        coords = geom_elem.find('.//gml:posList', self.namespaces).text.split()
        coords = [(float(coords[i]), float(coords[i + 1])) 
                 for i in range(0, len(coords), 2)]
        return Polygon(coords)

    def _parse_feature(self, feature):
        """Parse a single feature into a dictionary"""
        return {
            'id': feature.get('{http://www.opengis.net/gml/3.2}id'),
            'gridcode': int(feature.find('natur:gridcode', self.namespaces).text),
            'toerv_pct': feature.find('natur:toerv_pct', self.namespaces).text,
            'geometry': self._parse_geometry(
                feature.find('.//gml:Polygon', self.namespaces)
            )
        }

    async def _fetch_batch(self, session, start_index):
        """Fetch a batch of features"""
        params = self._get_params(start_index)
        async with session.get(self.config['url'], params=params) as response:
            if response.status != 200:
                raise Exception(f"Failed to fetch data: {response.status}")
            
            text = await response.text()
            root = ET.fromstring(text)
            features = root.findall('.//natur:kulstof2022', self.namespaces)
            
            return [self._parse_feature(feature) for feature in features]

    async def fetch(self):
        """Fetch all features and return as DataFrame"""
        async with aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0 QGIS/33603/macOS 15.1'}
        ) as session:
            # Get total count
            params = self._get_params(0)
            async with session.get(self.config['url'], params=params) as response:
                text = await response.text()
                root = ET.fromstring(text)
                total_features = int(root.get('numberMatched'))
                logger.info(f"Total features to fetch: {total_features}")
                
                # Process first batch
                first_batch = [self._parse_feature(f) for f in root.findall('.//natur:kulstof2022', self.namespaces)]
                logger.info(f"Fetched first batch: {len(first_batch)} features")

                # Fetch remaining batches
                tasks = []
                for start_index in range(self.batch_size, total_features, self.batch_size):
                    tasks.append(self._fetch_batch(session, start_index))
                    
                    # Process in chunks to manage memory
                    if len(tasks) >= self.max_concurrent:
                        chunk_results = await asyncio.gather(*tasks)
                        first_batch.extend([item for sublist in chunk_results for item in sublist])
                        tasks = []
                        logger.info(f"Processed {len(first_batch)} features so far")

                # Process any remaining tasks
                if tasks:
                    chunk_results = await asyncio.gather(*tasks)
                    first_batch.extend([item for sublist in chunk_results for item in sublist])

                logger.info(f"Total features processed: {len(first_batch)}")
                return gpd.GeoDataFrame(first_batch, crs="EPSG:25832")

    async def _create_tables(self, client):
        """Create necessary database tables"""
        await client.execute("""
            CREATE TABLE IF NOT EXISTS wetlands (
                id TEXT PRIMARY KEY,
                gridcode INTEGER,
                toerv_pct TEXT,
                geometry GEOMETRY(POLYGON, 25832)
            );
            
            CREATE INDEX IF NOT EXISTS wetlands_geometry_idx 
            ON wetlands USING GIST (geometry);
        """)

    async def _prepare_records(self, df):
        """Prepare records for insertion"""
        return [
            (
                row.id,
                row.gridcode,
                row.toerv_pct,
                row.geometry.wkt
            )
            for _, row in df.iterrows()
        ]

    async def _batch_insert(self, client, records):
        """Insert records in batches"""
        await client.executemany("""
            INSERT INTO wetlands (id, gridcode, toerv_pct, geometry)
            VALUES ($1, $2, $3, ST_GeomFromText($4, 25832))
            ON CONFLICT (id) DO UPDATE SET
                gridcode = EXCLUDED.gridcode,
                toerv_pct = EXCLUDED.toerv_pct,
                geometry = EXCLUDED.geometry
        """, records)

    async def sync(self, client):
        """Sync data to database"""
        logger.info("Creating tables...")
        await self._create_tables(client)

        logger.info("Fetching data...")
        df = await self.fetch()

        logger.info("Preparing records...")
        records = await self._prepare_records(df)

        logger.info("Inserting records...")
        insert_batch_size = 1000  # Smaller batch size for database insertions
        for i in range(0, len(records), insert_batch_size):
            batch = records[i:i + insert_batch_size]
            await self._batch_insert(client, batch)
            logger.info(f"Inserted {i + len(batch)} records")

        logger.info("Sync completed") 