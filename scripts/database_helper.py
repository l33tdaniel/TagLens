import sqlite3
import sqlite_vec
from sentence_transformers import SentenceTransformer
import json
from upload import *
# 1. Initialize the Embedding Model
embed_model = SentenceTransformer('all-MiniLM-L6-v2')

def init_db(db_path="data\\photos.db"):
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    
    cursor = conn.cursor()

    # Pillar 1: The Master Metadata Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS photos (
        photo_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        filepath TEXT,
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON photos(user_id)")

    # Pillar 2: Full-Text Search (Keyword Engine)
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
        loc_description,
        loc_city,
        loc_state,
        loc_country,
        caption,
        ocr_text,
        user_id UNINDEXED,
        content='photos',
        content_rowid='photo_id'
    )
    """)

    # Pillar 3: Vector Search (Semantic Engine)
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS photos_vec USING vec0(
        photo_id INTEGER PRIMARY KEY,
        user_id INTEGER,
        embedding FLOAT[384]
    )
    """)

    # Pillar 4: Dedicated Stats Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_stats (
        user_id INTEGER PRIMARY KEY,
        photo_count INTEGER DEFAULT 0
    )
    """)

    # Trigger: Added missing FTS columns so all location data is searchable
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS photos_ai AFTER INSERT ON photos BEGIN
        INSERT INTO photos_fts(rowid, loc_description, loc_city, loc_state, loc_country, caption, ocr_text, user_id)
        VALUES (new.photo_id, new.loc_description, new.loc_city, new.loc_state, new.loc_country, new.caption, new.ocr_text, new.user_id);
    END;
    """)

    # Trigger: Increment photo count
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS increment_photo_count 
    AFTER INSERT ON photos 
    BEGIN
        INSERT INTO user_stats (user_id, photo_count)
        VALUES (NEW.user_id, 1)
        ON CONFLICT(user_id) DO UPDATE SET photo_count = photo_count + 1;
    END;
    """)

    conn.commit()
    return conn

def save_photo_to_db(conn, metadata):
    cursor = conn.cursor()
    
    # 1. Insert into Master Table
    sql = """
    INSERT INTO photos (
        user_id, filepath, file_size_mb, width, height, 
        make, model, taken_at, iso, f_stop, shutter_speed, 
        focal_length, latitude, longitude, loc_description, loc_city,
        loc_state, loc_country, caption, ocr_text
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    
    values = (
        metadata["user_id"], metadata['filepath'], metadata['size'],
        metadata['w'], metadata['h'], metadata['make'], metadata['model'],
        metadata['date'], metadata['iso'], metadata['f_stop'],
        metadata['shutter'], metadata['focal'], metadata['lat'],
        metadata['lon'], metadata['loc_description'], metadata['loc_city'],
        metadata['loc_state'], metadata['loc_country'], metadata['caption'],
        metadata['ocr']
    )
    
    cursor.execute(sql, values)
    
    # Grab the newly generated integer ID
    photo_id = cursor.lastrowid

    ext = metadata["filename"].split(".")[-1]
    # Upload
    upload_to_b2(metadata['filepath'], f"{metadata["user_id"]}/{photo_id}.{ext}")

    # 2. Generate and Insert Embedding
    embedding = embed_model.encode(metadata['caption'])
    
    # Serialize the embedding for sqlite_vec & fixed placeholder count
    cursor.execute(
        "INSERT INTO photos_vec(photo_id, user_id, embedding) VALUES (?, ?, ?)",
        (photo_id, metadata["user_id"], sqlite_vec.serialize_float32(embedding))
    )

    conn.commit()
    print(f"Stored: {metadata['filepath']} with ID {photo_id} as {metadata["user_id"]}/{photo_id}.{ext}")