from pathlib import Path
import asyncio
import xml.etree.ElementTree as ET
import logging
import aiohttp
from shapely.geometry import Polygon, MultiPolygon
from datetime import datetime
import backoff
from aiohttp import ClientError, ClientTimeout
from ...base import Source, clean_value

logger = logging.getLogger(__name__)

class WaterProjects(Source):
    def __init__(self, config):
        super().__init__(config)
        self.batch_size = 100
        self.max_concurrent = 3
        self.request_timeout = 300
        
        self.create_combined = config.get('create_combined', True)
        self.combined_timeout = config.get('combined_timeout', 3600)
        
        self.request_timeout_config = ClientTimeout(
            total=self.request_timeout,
            connect=60,
            sock_read=300
        )
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 QGIS/33603/macOS 15.1'
        }
        
        self.layers = [
            "N2000_projekter:Hydrologi_E",
            "N2000_projekter:Hydrologi_F",
            "Ovrige_projekter:Vandloebsrestaurering_E",
            "Ovrige_projekter:Vandloebsrestaurering_F",
            "Vandprojekter:Fosfor_E_samlet",
            "Vandprojekter:Fosfor_F_samlet",
            "Vandprojekter:Kvaelstof_E_samlet",
            "Vandprojekter:Kvaelstof_F_samlet",
            "Vandprojekter:Lavbund_E_samlet",
            "Vandprojekter:Lavbund_F_samlet",
            "Vandprojekter:Private_vaadomraader",
            "Vandprojekter:Restaurering_af_aadale_2024",
            "vandprojekter:kla_projektforslag",
            "vandprojekter:kla_projektomraader"
        ]
        
        self.request_semaphore = asyncio.Semaphore(self.max_concurrent)
        
        self.url_mapping = {
            'vandprojekter:kla_projektforslag': 'https://wfs2-miljoegis.mim.dk/vandprojekter/wfs',
            'vandprojekter:kla_projektomraader': 'https://wfs2-miljoegis.mim.dk/vandprojekter/wfs'
        }

    def _get_params(self, layer, start_index=0):
        """Get WFS request parameters"""
        return {
            'SERVICE': 'WFS',
            'REQUEST': 'GetFeature',
            'VERSION': '2.0.0',
            'TYPENAMES': layer,
            'STARTINDEX': str(start_index),
            'COUNT': str(self.batch_size),
            'SRSNAME': 'urn:ogc:def:crs:EPSG::25832'
        }

    def _parse_geometry(self, geom_elem):
        """Parse GML geometry into WKT"""
        try:
            # Handle both the_geom and wkb_geometry element names
            gml_ns = '{http://www.opengis.net/gml/3.2}'
            
            # Find MultiSurface element
            multi_surface = geom_elem.find(f'.//{gml_ns}MultiSurface')
            if multi_surface is None:
                logger.error("No MultiSurface element found")
                return None
            
            polygons = []
            # Process each surface member
            for surface_member in multi_surface.findall(f'.//{gml_ns}surfaceMember'):
                polygon = surface_member.find(f'.//{gml_ns}Polygon')
                if polygon is None:
                    continue
                    
                # Get exterior ring coordinates
                pos_list = polygon.find(f'.//{gml_ns}posList')
                if pos_list is None or not pos_list.text:
                    continue
                    
                # Parse coordinates
                try:
                    coords = [float(x) for x in pos_list.text.strip().split()]
                    coords = [(coords[i], coords[i+1]) for i in range(0, len(coords), 2)]
                    polygons.append(Polygon(coords))
                except Exception as e:
                    logger.error(f"Failed to parse coordinates: {str(e)}")
                    continue
            
            if not polygons:
                return None
            
            return MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]
            
        except Exception as e:
            logger.error(f"Error parsing geometry: {str(e)}")
            return None

    def _parse_feature(self, feature, layer_name):
        """Parse a single feature into a dictionary"""
        try:
            # Get the namespace from the feature's tag
            namespace = feature.tag.split('}')[0].strip('{')
            
            # Handle geometry first
            geom_elem = feature.find(f'{{%s}}the_geom' % namespace) or feature.find(f'{{%s}}wkb_geometry' % namespace)
            if geom_elem is None:
                logger.warning(f"No geometry found in feature for layer {layer_name}")
                return None

            geometry = self._parse_geometry(geom_elem)
            if geometry is None:
                logger.warning(f"Failed to parse geometry for layer {layer_name}")
                return None

            # Base data
            data = {
                'layer_name': layer_name,
                'geometry': geometry
            }
            
            # Parse all other fields
            for elem in feature:
                if not elem.tag.endswith(('the_geom', 'wkb_geometry')):
                    key = elem.tag.split('}')[-1].lower()  # Get the attribute name without namespace
                    if elem.text:  # Only add non-empty values
                        data[key] = elem.text.strip()
            
            logger.debug(f"Parsed fields for layer {layer_name}: {list(data.keys())}")
            return data
            
        except Exception as e:
            logger.error(f"Error parsing feature in layer {layer_name}: {str(e)}", exc_info=True)
            return None

    @backoff.on_exception(
        backoff.expo,
        (ClientError, asyncio.TimeoutError),
        max_tries=3
    )
    async def _fetch_chunk(self, session, layer, start_index):
        """Fetch a chunk of features with retries"""
        async with self.request_semaphore:
            params = self._get_params(layer, start_index)
            url = self.url_mapping.get(layer, self.config['url'])
            
            try:
                async with session.get(
                    url, 
                    params=params,
                    timeout=self.request_timeout_config
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Error response from server: {error_text[:500]}")
                        raise ClientError(f"Server returned status {response.status}")
                    
                    text = await response.text()
                    logger.debug(f"Raw XML response for {layer} (first 500 chars): {text[:500]}")
                    
                    try:
                        root = ET.fromstring(text)
                    except ET.ParseError as e:
                        logger.error(f"XML Parse error for {layer}: {str(e)}")
                        logger.error(f"Problematic XML: {text[:1000]}")
                        raise
                    
                    # Log the root element and its immediate children
                    logger.debug(f"Root tag: {root.tag}")
                    logger.debug("Root's children tags:")
                    for child in root:
                        logger.debug(f"- {child.tag}")

                    # Find the correct namespace from the root element
                    namespaces = {}
                    for elem in root.iter():
                        if '}' in elem.tag:
                            ns_url = elem.tag.split('}')[0].strip('{')
                            namespaces['ns'] = ns_url
                            break

                    features = []
                    # Use direct tag matching instead of local-name()
                    for member in root.findall('.//ns:member', namespaces=namespaces):
                        for feature in member:
                            parsed = self._parse_feature(feature, layer)
                            if parsed and parsed['geometry']:
                                features.append(parsed)
                    
                    return features
                    
            except Exception as e:
                logger.error(f"Error fetching chunk for {layer} at index {start_index}")
                logger.error(f"Error details: {type(e).__name__}: {str(e)}")
                logger.debug("Stack trace:", exc_info=True)
                raise

    async def _create_tables(self, client):
        """Create necessary database tables"""
        # First check if table exists
        table_exists = await client.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'water_projects'
            )
        """)
        
        if not table_exists:
            await client.execute("""
                CREATE TABLE water_projects (
                    id SERIAL PRIMARY KEY,
                    layer_name TEXT,
                    area_ha NUMERIC,
                    journalnr TEXT,
                    titel TEXT,
                    ansoeger TEXT,
                    marknr TEXT,
                    cvr TEXT,
                    startaar INTEGER,
                    tilsagnsaa INTEGER,
                    slutaar INTEGER,
                    startdato DATE,
                    slutdato DATE,
                    ordning TEXT,
                    budget NUMERIC,
                    indsats TEXT,
                    projektn TEXT,
                    a_runde TEXT,
                    afgoer_fase2 TEXT,
                    projektgodk TEXT,
                    geometry GEOMETRY(GEOMETRY, 25832),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX water_projects_geometry_idx 
                ON water_projects USING GIST (geometry);
                
                CREATE INDEX water_projects_layer_idx 
                ON water_projects (layer_name);
            """)
        else:
            # Verify columns exist and add if missing
            await client.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                 WHERE table_name = 'water_projects' AND column_name = 'projektn') THEN
                        ALTER TABLE water_projects ADD COLUMN projektn TEXT;
                    END IF;
                    -- Add similar checks for other columns if needed
                END $$;
            """)
        
        if self.create_combined:
            await client.execute("""
                CREATE TABLE IF NOT EXISTS water_projects_combined (
                    id SERIAL PRIMARY KEY,
                    geometry GEOMETRY(MULTIPOLYGON, 25832),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS water_projects_combined_geometry_idx 
                ON water_projects_combined USING GIST (geometry);
            """)

    async def _insert_batch(self, client, features):
        """Insert a batch of features"""
        if not features:
            logger.warning("No features to insert in batch")
            return 0
        
        try:
            logger.info(f"Preparing to insert batch of {len(features)} features")
            
            values = []
            for f in features:
                try:
                    # Only look for area_ha since we normalized it during parsing
                    area = None
                    if 'area_ha' in f:
                        try:
                            area = float(f['area_ha'])
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid area value: {f['area_ha']}")
                
                    # Log the WKT representation
                    wkt = f['geometry'].wkt if f['geometry'] else None
                    if not wkt:
                        logger.warning("Empty geometry found in feature")
                        continue
                
                    values.append((
                        f['layer_name'],
                        area,
                        f.get('journalnr'),
                        f.get('titel'),
                        f.get('ansoeger'),
                        f.get('marknr'),
                        f.get('cvr'),
                        clean_value(f.get('startaar')),
                        clean_value(f.get('tilsagnsaa')),
                        clean_value(f.get('slutaar')),
                        datetime.strptime(f.get('startdato', ''), '%d-%m-%Y').date() if f.get('startdato') else None,
                        datetime.strptime(f.get('slutdato', ''), '%d-%m-%Y').date() if f.get('slutdato') else None,
                        f.get('ordning'),
                        f.get('budget'),
                        f.get('indsats'),
                        f.get('projektn'),
                        f.get('a_runde'),
                        f.get('afgoer_fase2'),
                        f.get('projektgodk'),
                        wkt
                    ))
                except Exception as e:
                    logger.error(f"Error preparing feature for insert: {str(e)}")
                    continue

            if not values:
                logger.warning("No valid values to insert after processing")
                return 0

            logger.info(f"Executing insert for {len(values)} features")
            
            # Execute the insert
            await client.executemany("""
                INSERT INTO water_projects (
                    layer_name, area_ha, journalnr, titel, 
                    ansoeger, marknr, cvr, startaar, tilsagnsaa, slutaar,
                    startdato, slutdato, ordning, budget, indsats,
                    projektn, a_runde, afgoer_fase2, projektgodk,
                    geometry
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 
                        $12, $13, $14, $15, $16, $17, $18, $19,
                        ST_GeomFromText($20, 25832))
            """, values)
            
            logger.info(f"Successfully inserted {len(values)} features")
            return len(values)
            
        except Exception as e:
            logger.error(f"Error inserting batch: {str(e)}", exc_info=True)
            if features:
                logger.error(f"First feature data: {features[0]}")
            return 0

    async def sync(self, client):
        """Sync all water project layers"""
        total_processed = 0  # Initialize counter
        
        try:
            # Create tables if they don't exist
            await self._create_tables(client)
            
            async with aiohttp.ClientSession(headers=self.headers) as session:
                for layer in self.layers:
                    logger.info(f"\nProcessing layer: {layer}")
                    try:
                        # Use the correct URL based on the layer
                        base_url = self.url_mapping.get(layer, self.config['url'])
                        
                        # Get initial batch to determine total count
                        params = self._get_params(layer, 0)
                        async with session.get(base_url, params=params) as response:
                            if response.status != 200:
                                logger.error(f"Failed to fetch {layer}. Status: {response.status}")
                                error_text = await response.text()
                                logger.error(f"Error response: {error_text[:500]}")
                                continue
                            
                            text = await response.text()
                            root = ET.fromstring(text)
                            total_features = int(root.get('numberMatched', '0'))
                            logger.info(f"Layer {layer}: found {total_features:,} total features")
                            
                            # Process first batch
                            features = []
                            namespaces = {}
                            for elem in root.iter():
                                if '}' in elem.tag:
                                    ns_url = elem.tag.split('}')[0].strip('{')
                                    namespaces['ns'] = ns_url
                                    break
                                    
                            for member in root.findall('.//ns:member', namespaces=namespaces):
                                for feature in member:
                                    parsed = self._parse_feature(feature, layer)
                                    if parsed and parsed['geometry']:
                                        features.append(parsed)
                            
                            if features:
                                inserted = await self._insert_batch(client, features)
                                if inserted:  # Ensure inserted is not None
                                    total_processed += inserted
                                logger.info(f"Layer {layer}: inserted {inserted:,} features. Total processed: {total_processed:,}")
                            
                            # Process remaining batches
                            for start_index in range(self.batch_size, total_features, self.batch_size):
                                logger.info(f"Layer {layer}: fetching features {start_index:,}-{min(start_index + self.batch_size, total_features):,} of {total_features:,}")
                                features = await self._fetch_chunk(session, layer, start_index)
                                if features:
                                    inserted = await self._insert_batch(client, features)
                                    if inserted:  # Ensure inserted is not None
                                        total_processed += inserted
                                    logger.info(f"Layer {layer}: inserted {inserted:,} features. Total processed: {total_processed:,}")
                                
                    except Exception as e:
                        logger.error(f"Error processing layer {layer}: {str(e)}", exc_info=True)
                        continue
            
            return total_processed
        except Exception as e:
            logger.error(f"Error in sync: {str(e)}", exc_info=True)
            return total_processed  # Return current count even if error occurs

    async def fetch(self):
        """Not implemented - using sync() directly"""
        raise NotImplementedError("This source uses sync() directly") 