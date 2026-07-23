import os
import secrets
import sqlite3
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import groq
from tavily import TavilyClient

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = ""

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get(user_id):
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        return User(row["id"], row["username"], row["password_hash"]) if row else None

    @staticmethod
    def get_by_username(username):
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        return User(row["id"], row["username"], row["password_hash"]) if row else None

    @staticmethod
    def create(username, password):
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password))
            )
            conn.commit()
            user = User.get_by_username(username)
            conn.close()
            return user
        except Exception:
            conn.close()
            return None


@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)


def get_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    return groq.Groq(api_key=api_key)

def get_tavily_client():
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return None
    return TavilyClient(api_key=api_key)


MODEL = "llama-3.1-8b-instant"


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=current_user.username)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please fill in all fields.")
        elif len(username) < 3:
            flash("Username must be at least 3 characters.")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.")
        elif User.get_by_username(username):
            flash("Username already taken.")
        else:
            user = User.create(username, password)
            if user:
                login_user(user)
                return redirect(url_for("index"))
            flash("Registration failed.")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.get_by_username(username)
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        flash("Invalid username or password.")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    try:
        client = get_client()
        if not client:
            return jsonify({"response": "Error: GROQ_API_KEY not set. Please add it in Vercel settings."})

        data = request.json
        prompt = data.get("prompt", "")
        max_tokens = data.get("max_tokens", 1024)
        use_web_search = data.get("use_web_search", False)
        chat_id = data.get("chat_id")

        system_content = "You are a helpful assistant."

        if use_web_search:
            tavily = get_tavily_client()
            if not tavily:
                return jsonify({"response": "Error: TAVILY_API_KEY not set. Please add it in Vercel settings."})
            search_result = tavily.search(query=prompt, search_depth="basic")
            context = "\n\n".join(
                [r.get("content", "") for r in search_result.get("results", [])]
            )
            system_content = (
                "You are a helpful assistant with access to real-time web search results. "
                "Use the following web search context to answer the user's question accurately. "
                "If the context is insufficient, say so.\n\n"
                f"Web Search Results:\n{context}"
            )

        messages = [{"role": "system", "content": system_content}]

        if chat_id:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at ASC",
                (chat_id,)
            )
            for role, content in cur.fetchall():
                messages.append({"role": role, "content": content})
            conn.close()

        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            top_p=0.9,
        )

        return jsonify({"response": response.choices[0].message.content})

    except Exception as e:
        return jsonify({"response": f"Error: {str(e)}"})


@app.route("/chats", methods=["GET"])
@login_required
def get_chats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created_at FROM chats WHERE user_id = ? ORDER BY created_at DESC",
        (current_user.id,)
    )
    chats = [{"id": r[0], "title": r[1], "created_at": r[2]} for r in cur.fetchall()]
    conn.close()
    return jsonify(chats)


@app.route("/chats", methods=["POST"])
@login_required
def create_chat():
    data = request.json or {}
    title = data.get("title", "New chat")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chats (user_id, title) VALUES (?, ?) RETURNING id, title, created_at",
        (current_user.id, title)
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return jsonify({"id": row[0], "title": row[1], "created_at": row[2]})


@app.route("/chats/<int:chat_id>", methods=["PUT"])
@login_required
def update_chat(chat_id):
    data = request.json
    conn = get_db()
    conn.execute(
        "UPDATE chats SET title = ? WHERE id = ? AND user_id = ?",
        (data["title"], chat_id, current_user.id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/chats/<int:chat_id>", methods=["DELETE"])
@login_required
def delete_chat(chat_id):
    conn = get_db()
    conn.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, current_user.id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/chats/<int:chat_id>/messages", methods=["GET"])
@login_required
def get_messages(chat_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at ASC",
        (chat_id,)
    )
    messages = [{"role": r[0], "content": r[1]} for r in cur.fetchall()]
    conn.close()
    return jsonify(messages)


@app.route("/chats/<int:chat_id>/messages", methods=["POST"])
@login_required
def save_message(chat_id):
    data = request.json
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, data["role"], data["content"])
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


init_db()
