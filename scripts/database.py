import sqlite3
import sqlite_vec
from sentence_transformers import SentenceTransformer
import json

# 1. Initialize the Embedding Model (Lightweight & Fast)
# This turns your captions into a 384-dimension vector
embed_model = SentenceTransformer('all-MiniLM-L6-v2')

def init_db(db_path="database\\photos.db"):
    conn = sqlite3.connect(db_path)
    # Load the sqlite-vec extension
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    
    cursor = conn.cursor()

    # Pillar 1: The Master Metadata Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS photos (
        photo_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        user_ID INTEGER NOT NULL,
        filename TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_size_mb REAL,
        width INTEGER, height INTEGER,
        make TEXT, model TEXT,
        taken_at DATETIME,
        iso INTEGER, f_stop REAL, shutter_speed TEXT, focal_length REAL,
        latitude REAL, longitude REAL,
        loc_description TEXT, 
        loc_city TEXT,
        loc_state TEXT, 
        loc_country TEXT,
        caption TEXT,
        ocr_text TEXT
    )
    """)

    # 2. CREATE INDEX for fast user lookups
    # This makes filtering by user_id lightning fast
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON photos(user_id)")

    # Pillar 2: Full-Text Search (Keyword Engine)
    # We use external content to keep the DB size smaller
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
        loc_description,
        loc_city,
        caption,
        content='photos',
        content_rowid='id'
    )
    """)

    # Pillar 3: Vector Search (Semantic Engine)
    # 384 dimensions matches the MiniLM model used above
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS photos_vec USING vec0(
        id INTEGER PRIMARY KEY,
        embedding FLOAT[384]
    )
    """)

    # Triggers: Automatically update the Keyword Index when a photo is added
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS photos_ai AFTER INSERT ON photos BEGIN
        INSERT INTO photos_fts(rowid, loc_description, caption)
        VALUES (new.id, new.loc_description, new.caption);
    END;
    """)

    conn.commit()
    return conn

# WIP
def save_photo_to_db(conn, metadata):
    cursor = conn.cursor()
    
    # 1. Insert into Master Table
    sql = """
    INSERT INTO photos (
        filename, file_path, file_size_mb, width, height, 
        make, model, taken_at, iso, f_stop, shutter_speed, 
        focal_length, latitude, longitude, loc_description, 
        loc_state, loc_country, caption
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    
    # Split location_data logic (matching your reverse_geocoder output)
    # Assuming location_data is a list/dict from your previous code
    loc_raw = metadata.get('location_data', [{}])[0]
    loc_desc = f"{loc_raw.get('name', '')}, {loc_raw.get('admin2', '')}"
    loc_state = loc_raw.get('admin1', 'Unknown')
    loc_country = loc_raw.get('cc', 'Unknown')

    values = (
        metadata['filename'], metadata['path'], metadata['size'],
        metadata['w'], metadata['h'], metadata['make'], metadata['model'],
        metadata['date'], metadata['iso'], metadata['f_stop'],
        metadata['shutter'], metadata['focal'], metadata['lat'],
        metadata['lon'], loc_desc, loc_state, loc_country, metadata['caption']
    )
    
    cursor.execute(sql, values)
    photo_id = cursor.lastrowid

    # 2. Generate and Insert Embedding
    embedding = embed_model.encode(metadata['caption'])
    # sqlite-vec expects a serializable format for the vector
    cursor.execute(
        "INSERT INTO photos_vec(id, embedding) VALUES (?, ?)",
        (photo_id, embedding)
    )
    
    conn.commit()
    print(f"Stored: {metadata['filename']} with ID: {photo_id}")