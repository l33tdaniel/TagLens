import sqlite3
import sqlite_vec
from sentence_transformers import SentenceTransformer
from scripts.upload import upload_to_b2

# 1. Initialize the Embedding Model
embed_model = SentenceTransformer("all-MiniLM-L6-v2")


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
        is_photo BOOLEAN,
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

    # Force the AUTOINCREMENT to start at a 6-digit number (100000)
    # This seeds the hidden sqlite_sequence table with 99999 so the next ID is 100000.
    cursor.execute("""
    INSERT INTO sqlite_sequence (name, seq) 
    SELECT 'photos', 99999 
    WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'photos');
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

    # Pillar 4: Dedicated Stats Table (Now with video_count)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_stats (
        user_id INTEGER PRIMARY KEY,
        photo_count INTEGER DEFAULT 0,
        video_count INTEGER DEFAULT 0
    )
    """)

    # Trigger: FTS only fires when is_photo is True (1)
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS photos_ai AFTER INSERT ON photos 
    WHEN new.is_photo = 1
    BEGIN
        INSERT INTO photos_fts(rowid, loc_description, loc_city, loc_state, loc_country, caption, ocr_text, user_id)
        VALUES (new.photo_id, new.loc_description, new.loc_city, new.loc_state, new.loc_country, new.caption, new.ocr_text, new.user_id);
    END;
    """)

    # Trigger: Increment photo count only if it's a photo
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS increment_photo_count 
    AFTER INSERT ON photos 
    WHEN new.is_photo = 1
    BEGIN
        INSERT INTO user_stats (user_id, photo_count, video_count)
        VALUES (NEW.user_id, 1, 0)
        ON CONFLICT(user_id) DO UPDATE SET photo_count = photo_count + 1;
    END;
    """)

    # Trigger: Increment video count only if it's a video
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS increment_video_count 
    AFTER INSERT ON photos 
    WHEN new.is_photo = 0
    BEGIN
        INSERT INTO user_stats (user_id, photo_count, video_count)
        VALUES (NEW.user_id, 0, 1)
        ON CONFLICT(user_id) DO UPDATE SET video_count = video_count + 1;
    END;
    """)

    conn.commit()
    return conn


def save_photo_to_db(conn, metadata):
    cursor = conn.cursor()

    # 1. Insert into Master Table (is_photo hardcoded to True)
    sql = """
    INSERT INTO photos (
        user_id, filepath, is_photo, file_size_mb, width, height, 
        make, model, taken_at, iso, f_stop, shutter_speed, 
        focal_length, latitude, longitude, loc_description, loc_city,
        loc_state, loc_country, caption, ocr_text
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    values = (
        metadata["user_id"],
        metadata["filepath"],
        True,
        metadata["size"],
        metadata["w"],
        metadata["h"],
        metadata["make"],
        metadata["model"],
        metadata["date"],
        metadata["iso"],
        metadata["f_stop"],
        metadata["shutter"],
        metadata["focal"],
        metadata["lat"],
        metadata["lon"],
        metadata["loc_description"],
        metadata["loc_city"],
        metadata["loc_state"],
        metadata["loc_country"],
        metadata["caption"],
        metadata["ocr"],
    )

    cursor.execute(sql, values)
    photo_id = cursor.lastrowid

    ext = metadata["filename"].split(".")[-1]
    upload_to_b2(metadata["filepath"], f"{metadata['user_id']}/{photo_id}.{ext}")

    # 2. Generate and Insert Embedding (Only happens for photos)
    embedding = embed_model.encode(metadata["caption"])

    cursor.execute(
        "INSERT INTO photos_vec(photo_id, user_id, embedding) VALUES (?, ?, ?)",
        (photo_id, metadata["user_id"], sqlite_vec.serialize_float32(embedding)),
    )

    conn.commit()
    try:
        print(
            f"Stored Photo: {metadata['filepath']} with ID {photo_id} as {metadata['user_id']}/{photo_id}.{ext}"
        )
    except UnicodeEncodeError:
        print("UTF Error")
        print(
            f"Stored Photo: {repr(metadata['filepath'])} with ID {photo_id} as {metadata['user_id']}/{photo_id}.{ext}"
        )


def save_video_to_db(conn, metadata):
    cursor = conn.cursor()

    # 1. Insert Video into Master Table (is_photo hardcoded to False, most fields None)
    sql = """
    INSERT INTO photos (
        user_id, filepath, is_photo, file_size_mb, width, height, 
        make, model, taken_at, iso, f_stop, shutter_speed, 
        focal_length, latitude, longitude, loc_description, loc_city,
        loc_state, loc_country, caption, ocr_text
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    values = (
        metadata["user_id"],
        metadata["filepath"],
        False,
        metadata["size"],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    cursor.execute(sql, values)
    video_id = cursor.lastrowid

    # 2. Upload to B2
    ext = metadata["filename"].split(".")[-1]
    upload_to_b2(metadata["filepath"], f"{metadata['user_id']}/{video_id}.{ext}")

    # Note: No embedding or vector search logic here.

    conn.commit()
    try:
        print(
            f"Stored Video: {metadata['filepath']} with ID {video_id} as {metadata['user_id']}/{video_id}.{ext}"
        )
    except UnicodeEncodeError:
        print("UTF Error")
        print(
            f"Stored Video: {repr(metadata['filepath'])} with ID {video_id} as {metadata['user_id']}/{video_id}.{ext}"
        )
