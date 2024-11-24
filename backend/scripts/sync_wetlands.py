import asyncio
import os
from pathlib import Path
import sys
import asyncpg
from dotenv import load_dotenv

# Add the backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.append(str(backend_dir))

from src.sources.parsers.wetlands import Wetlands
from src.config import SOURCES

async def main():
    """Sync wetlands data to PostgreSQL"""
    load_dotenv()
    
    db_host = os.getenv('DB_HOST')
    db_name = os.getenv('DB_NAME')
    db_user = os.getenv('DB_USER')
    db_password = os.getenv('DB_PASSWORD')
    
    if not all([db_host, db_name, db_user, db_password]):
        raise ValueError("Missing database configuration")
    
    conn = None
    try:
        conn = await asyncpg.connect(
            host=db_host,
            database=db_name,
            user=db_user,
            password=db_password
        )
        print("Database connection established")
        
        wetlands = Wetlands(SOURCES["wetlands"])
        await wetlands.sync(conn)
        
    except Exception as e:
        print(f"Error connecting to database: {str(e)}")
        raise
    finally:
        if conn:
            await conn.close()

if __name__ == "__main__":
    asyncio.run(main())