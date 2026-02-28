import sqlite3
import sqlite_vec


def check_db_contents(db_path="data\\photos.db"):
    # 1. Connect and load the vector extension
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    # This trick lets us access columns by name and print them as dictionaries
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # --- 1. Check Master Photos Table ---
    print("\n=== PHOTOS TABLE ===")
    cursor.execute("SELECT * FROM photos")
    photos = cursor.fetchall()
    if not photos:
        print("No photos found.")
    for p in photos:
        print(dict(p))

    # --- 2. Check User Stats ---
    print("\n=== USER STATS ===")
    cursor.execute("SELECT * FROM user_stats")
    stats = cursor.fetchall()
    if not stats:
        print("No user stats found.")
    for s in stats:
        print(dict(s))

    # --- 3. Check Full-Text Search Table ---
    print("\n=== FTS5 KEYWORD INDEX ===")
    # We select the rowid to ensure it matches the photo_id from the master table
    cursor.execute("SELECT * FROM photos_fts")
    fts_rows = cursor.fetchall()
    if not fts_rows:
        print("No FTS data found.")
    for f in fts_rows:
        print(dict(f))

    # --- 4. Check Vector Table ---
    print("\n=== VECTOR EMBEDDINGS ===")
    # Note: We purposely DO NOT select the 'embedding' column here.
    # Printing an array of 384 floats for every single photo will completely flood your terminal.
    # Instead, we check the standard SQLite length() to ensure the blob data actually saved.
    cursor.execute(
        "SELECT photo_id, user_id, length(embedding) as embedding_byte_size FROM photos_vec"
    )
    vec_rows = cursor.fetchall()
    if not vec_rows:
        print("No vector data found.")
    for v in vec_rows:
        print(dict(v))

    conn.close()


def check_db_filepath(db_path="data\\photos.db"):
    # 1. Connect and load the vector extension
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    # This trick lets us access columns by name and print them as dictionaries
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # --- 1. Check Master Photos Table ---
    cursor.execute("SELECT filepath FROM photos")
    filepaths = {row[0] for row in cursor.fetchall()}

    conn.close()
    return filepaths


def check(db_path="data\\photos.db"):
    # 1. Connect and load the vector extension
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    # This trick lets us access columns by name and print them as dictionaries
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # --- 1. Check Master Photos Table ---
    print("\n=== PHOTOS TABLE ===")
    cursor.execute("SELECT * FROM photos ORDER BY photo_id DESC LIMIT 5")
    filepaths = cursor.fetchall()
    for p in filepaths:
        print(dict(p))

    conn.close()
    return filepaths


# Run the diagnostic
if __name__ == "__main__":
    check()
