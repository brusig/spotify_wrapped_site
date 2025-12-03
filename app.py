# Import necessary libraries
from flask import Flask, render_template, request, redirect, url_for, g, session, jsonify
import sqlite3  # For SQLite database
import random  # For picking random people
import os      # For handling file paths

# OLD:
# BASE_DIR = os.path.dirname(__file__)
# DB_PATH = os.path.join(BASE_DIR, "data.db")

# NEW — Store the database on Render's persistent disk:
DB_DIR = "/var/data"
os.makedirs(DB_DIR, exist_ok=True)   # ensure directory exists (safe locally too)

DB_PATH = os.path.join(DB_DIR, "data.db")


# Initialize Flask app
app = Flask(__name__)
# Use env-provided secret for sessions; fallback for local dev
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "replace_with_a_random_secret")
# Session cookie hardening (secure only on production)
is_prod = os.environ.get("FLASK_ENV") == "production"
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_prod,
)

# -------------------------------
# DATABASE HELPER FUNCTIONS
# -------------------------------

# Get a database connection for the current request (or create it)
def get_db():
    db = getattr(g, "_database", None)  # Check if a connection is already stored in `g`
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)  # Open a new connection
        db.row_factory = sqlite3.Row  # Rows can be accessed as dicts (row["name"])
        # Ensure SQLite enforces FK constraints and uses WAL for better concurrency
        db.execute("PRAGMA foreign_keys = ON;")
        db.execute("PRAGMA journal_mode = WAL;")
    return db

# Initialize database tables if they don't exist
def init_db():
    db = get_db()
    # Table for people
    db.execute("""CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE
                  )""")
    # Table for tracks (3 per person)
    db.execute("""CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL,
                    track_id TEXT NOT NULL,
                    pos INTEGER NOT NULL,
                    FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
                  )""")
    # Helpful indexes
    db.execute("CREATE INDEX IF NOT EXISTS idx_tracks_person_pos ON tracks(person_id, pos)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tracks_trackid ON tracks(track_id)")
    # Table for leaderboard entries
    db.execute("""CREATE TABLE IF NOT EXISTS leaderboard (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    right INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    percent INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )""")
    # Table to track how often each person is guessed correctly (for chameleon award)
    db.execute("""CREATE TABLE IF NOT EXISTS person_stats (
                    person_id INTEGER PRIMARY KEY,
                    correct_guesses INTEGER DEFAULT 0,
                    total_guesses INTEGER DEFAULT 0,
                    FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
                  )""")
    db.commit()  # Save changes

# Close DB connection at the end of each request
@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db:
        db.close()

# Initialize database once on the first incoming request (Flask 3 compatibility)
@app.before_request
def setup():
    if not app.config.get("DB_INITIALIZED", False):
        init_db()
        app.config["DB_INITIALIZED"] = True

# -------------------------------
# ROUTES
# -------------------------------

# Redirect root URL to /quiz
@app.route("/")
def index():
    return redirect(url_for("quiz"))

# -------------------------------
# INPUT SONGS ROUTE — add your top songs
# -------------------------------
@app.route("/input-songs", methods=["GET", "POST"])
def input_songs():
    db = get_db()

    if request.method == "POST":
        # Get form data
        name = request.form["name"].strip()
        t1 = request.form["t1"].strip()
        t2 = request.form["t2"].strip()
        t3 = request.form["t3"].strip()

        # Validate inputs
        if not name or not (t1 and t2 and t3):
            return "Name and three tracks required", 400

        # Insert person if not exists
        db.execute("INSERT OR IGNORE INTO people (name) VALUES (?)", (name,))
        db.commit()

        # Get person's ID
        person = db.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        pid = person["id"]

        # Remove any existing tracks (for editing)
        db.execute("DELETE FROM tracks WHERE person_id = ?", (pid,))
        # Insert the 3 tracks
        db.executemany("INSERT INTO tracks (person_id, track_id, pos) VALUES (?, ?, ?)",
                       [(pid, extract_track_id(t1), 1),
                        (pid, extract_track_id(t2), 2),
                        (pid, extract_track_id(t3), 3)])
        db.commit()
        return redirect(url_for("quiz"))

    # GET request: show all people who have added songs
    people = db.execute("SELECT * FROM people ORDER BY name").fetchall()
    return render_template("input_songs.html", people=people)

# -------------------------------
# HELPER — extract Spotify track ID
# -------------------------------
def extract_track_id(s):
    # Accept full spotify URL or just the ID
    if "open.spotify.com/track/" in s:
        try:
            part = s.split("open.spotify.com/track/")[1]
            tid = part.split("?")[0].split("/")[0]
            return tid
        except:
            return s
    return s

# -------------------------------
# QUIZ ROUTE — show random person's top 3 tracks
# -------------------------------
@app.route("/quiz", methods=["GET"])
def quiz():
    db = get_db()
    # Only include players who have 3 tracks to ensure valid rounds
    people = db.execute(
        """
        SELECT p.* FROM people p
        WHERE (
          SELECT COUNT(1) FROM tracks t WHERE t.person_id = p.id
        ) >= 3
        """
    ).fetchall()

    # No people added yet → show friendly empty state with CTA to add songs
    if not people:
        return render_template("empty.html")
    # Require at least 3 players for fair multiple choice without decoys
    if len(people) < 3:
        return render_template("need_more_players.html", count=len(people))

    # Already shown everyone? Go to finished (modal on finished will ask for name)
    if session.get("all_shown"):
        return redirect(url_for("finished"))

    # Get or initialize remaining people for this session
    remaining = session.get("remaining_people")
    if remaining is None:  # Only initialize if not set at all
        remaining = [p["id"] for p in people]
        session["remaining_people"] = remaining

    # Pick a random person for this round
    person_id = random.choice(remaining)
    session["current_person_id"] = person_id

    # Fetch that person's tracks
    person = db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    tracks = db.execute("SELECT track_id FROM tracks WHERE person_id=? ORDER BY pos", (person_id,)).fetchall()
    track_ids = [t["track_id"] for t in tracks]

    # Get the correct person's name
    correct_person = db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
    correct_name = correct_person["name"]

    # Get all correctly guessed people so far
    guessed_correctly = []
    history = session.get("history", [])
    for guess in history:
        if guess.get("is_right", False):
            guessed_correctly.append(guess["correct"])

    # Build distractors from real players (we have at least 3 players now)
    other_names = [p["name"] for p in people if p["id"] != person_id]
    distractors = random.sample(other_names, 2)

    # Combine correct name with distractors and shuffle
    names = [correct_name] + distractors
    random.shuffle(names)  # Shuffle so correct answer isn't always in same position

    # Store correct person ID in session for /guess
    session["correct_person_id"] = person_id
    # Store the track ids for the current round so /guess can save them into history
    session["current_track_ids"] = track_ids

    # Get or set total number of people in session for progress calculation
    if 'total_people' not in session:
        total_people = len(db.execute("SELECT id FROM people").fetchall())
        session['total_people'] = total_people
    
    # Get current progress
    score = session.get("score", {"right": 0, "total": 0})
    progress = {
        'score': score,
        'total_people': session['total_people'],
        'percent': int((score['total'] / session['total_people']) * 100) if session['total_people'] > 0 else 0
    }

    return render_template("quiz.html", track_ids=track_ids, choices=names, progress=progress)

# -------------------------------
# GUESS ROUTE — check answer
# -------------------------------
@app.route("/guess", methods=["POST"])
def guess():
    chosen = request.form.get("choice")
    db = get_db()
    correct_id = session.get("correct_person_id")
    if correct_id is None:
        return redirect(url_for("quiz"))

    correct_row = db.execute("SELECT name FROM people WHERE id=?", (correct_id,)).fetchone()
    correct_name = correct_row["name"] if correct_row else "Unknown"
    result = (chosen == correct_name)

    # Update person stats for chameleon award
    # Check if stats exist for this person
    existing = db.execute("SELECT * FROM person_stats WHERE person_id = ?", (correct_id,)).fetchone()
    
    if existing:
        # Update existing stats
        if result:
            db.execute("""
                UPDATE person_stats 
                SET correct_guesses = correct_guesses + 1, total_guesses = total_guesses + 1 
                WHERE person_id = ?
            """, (correct_id,))
        else:
            db.execute("""
                UPDATE person_stats 
                SET total_guesses = total_guesses + 1 
                WHERE person_id = ?
            """, (correct_id,))
    else:
        # Insert new stats
        if result:
            db.execute("""
                INSERT INTO person_stats (person_id, correct_guesses, total_guesses) 
                VALUES (?, 1, 1)
            """, (correct_id,))
        else:
            db.execute("""
                INSERT INTO person_stats (person_id, correct_guesses, total_guesses) 
                VALUES (?, 0, 1)
            """, (correct_id,))
    db.commit()

    # update per-session score and remove person from remaining
    score = session.get("score", {"right": 0, "total": 0})
    score["total"] += 1
    if result:
        score["right"] += 1
    session["score"] = score

    # Remove the person from remaining after guess is made
    remaining = session.get("remaining_people", [])
    current_person_id = session.get("current_person_id")
    if current_person_id in remaining:
        remaining.remove(current_person_id)
        if remaining:  # If we still have people to guess
            session["remaining_people"] = remaining
        else:  # No more people to guess
            session["remaining_people"] = None
            session["all_shown"] = True

    # --- append this round to history (create history if missing) ---
        # --- append this round to history (create history if missing) ---
    history = session.get("history", [])
    round_tracks = session.get("current_track_ids", [])  # list of track ids for the round
    history.append({
        "chosen": chosen,
        "correct": correct_name,
        "is_right": result,
        "tracks": round_tracks
    })
    session["history"] = history

    # cleanup current round data (so refreshing won't duplicate)
    session.pop("current_track_ids", None)
    session.pop("correct_person_id", None)

    # store last result for feedback
    session["last_result"] = {"chosen": chosen, "correct": correct_name, "is_right": result}

    # Return JSON response
    return jsonify({
        "chosen": chosen,
        "correct": correct_name,
        "is_right": result,
        "score": score,
        "finished": session.get("all_shown", False)
    })



# -------------------------------
# FINISHED ROUTE — show final score
# -------------------------------
@app.route("/finished", methods=["GET"])
def finished():
    # Decide if we should show the name entry modal
    show_name_modal = bool(session.get("all_shown") and not session.get("name_submitted"))
    score = session.get("score", {"right": 0, "total": 0})
    last = session.get("last_result")
    history = session.get("history", [])

    # Analyze shared tracks
    db = get_db()
    
    # Get all tracks and who they belong to
    tracks = db.execute("""
        SELECT t.track_id, p.name 
        FROM tracks t 
        JOIN people p ON t.person_id = p.id 
        ORDER BY t.track_id
    """).fetchall()

    # Group tracks by track_id and collect all owners
    shared_tracks = {}
    for track in tracks:
        tid = track["track_id"]
        if tid not in shared_tracks:
            shared_tracks[tid] = {"owners": []}
        shared_tracks[tid]["owners"].append(track["name"])

    # Filter to only tracks that appear for multiple people and sort by number of owners
    shared_tracks = {
        tid: data for tid, data in shared_tracks.items() 
        if len(data["owners"]) > 1
    }
    
    # Convert to sorted list of tuples (track_id, data) by number of owners
    shared_tracks = sorted(
        shared_tracks.items(),
        key=lambda x: len(x[1]["owners"]),
        reverse=True
    )

    # Leaderboard entries for display on finished page
    leaderboard_entries = db.execute(
        """
        SELECT name, right, total, percent, created_at
        FROM leaderboard
        ORDER BY percent DESC, right DESC, created_at ASC
        LIMIT 5
        """
    ).fetchall()

    # Find the "Best Chameleon" - person with lowest correct guess rate (minimum 3 total guesses)
    chameleon = db.execute("""
        SELECT p.name, ps.correct_guesses, ps.total_guesses,
               ROUND((ps.correct_guesses * 100.0 / ps.total_guesses), 1) as success_rate
        FROM person_stats ps
        JOIN people p ON ps.person_id = p.id
        WHERE ps.total_guesses >= 3
        ORDER BY success_rate ASC, ps.total_guesses DESC
        LIMIT 1
    """).fetchone()

    return render_template(
        "finished.html",
        score=score,
        last=last,
        history=history,
        shared_tracks=shared_tracks,
        leaderboard_entries=leaderboard_entries,
        chameleon=chameleon,
        show_name_modal=show_name_modal
    )


# -------------------------------
# ENTER NAME ROUTES — save score to leaderboard
# -------------------------------
@app.route("/enter-name", methods=["GET", "POST"])
def enter_name():
    # Only allow if the user has completed the quiz
    if not session.get("all_shown"):
        return redirect(url_for("quiz"))

    if request.method == "POST":
        player_name = request.form.get("name", "").strip()
        if not player_name:
            return render_template("enter_name.html", error="Please enter your name.")

        score = session.get("score", {"right": 0, "total": 0})
        right = int(score.get("right", 0))
        total = int(score.get("total", 0))
        percent = int((right / total) * 100) if total > 0 else 0

        db = get_db()
        db.execute(
            "INSERT INTO leaderboard (name, right, total, percent) VALUES (?, ?, ?, ?)",
            (player_name, right, total, percent),
        )
        db.commit()

        # mark name submission for this session
        session["name_submitted"] = True

        return redirect(url_for("finished"))

    # GET: not used in modal flow — fall back to finished where modal will open
    return redirect(url_for("finished"))


@app.route("/skip-name", methods=["POST"])
def skip_name():
    # User chose to skip adding their name for this session
    if session.get("all_shown") and not session.get("name_submitted"):
        session["name_submitted"] = True
    return redirect(url_for("finished"))


# -------------------------------
# LEADERBOARD — show top scores
# -------------------------------
@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    db = get_db()
    # Order by percent desc, then right desc, then earliest created_at
    rows = db.execute(
        """
        SELECT name, right, total, percent, created_at
        FROM leaderboard
        ORDER BY percent DESC, right DESC, created_at ASC
        """
    ).fetchall()
    return render_template("leaderboard.html", entries=rows)


# -------------------------------
# RESET ROUTE — clears quiz state
# -------------------------------
@app.route("/reset", methods=["POST"])
def reset():
    # Clear only the quiz-related session keys so admin login or other session data remains if present
    for k in ("remaining_people", "all_shown", "correct_person_id", "last_result", "score", "history", "current_track_ids", "name_submitted"):
        session.pop(k, None)

    return redirect(url_for("quiz"))


# -------------------------------
# RUN APP
# -------------------------------
if __name__ == "__main__":
    app.run(debug=True)
