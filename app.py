import os
import secrets
import threading
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import groq
from tavily import TavilyClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = ""

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = False
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memory (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, key)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get(user_id):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return User(row["id"], row["username"], row["password_hash"]) if row else None

    @staticmethod
    def get_by_username(username):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return User(row["id"], row["username"], row["password_hash"]) if row else None

    @staticmethod
    def create(username, password):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING *",
                (username, generate_password_hash(password))
            )
            row = cur.fetchone()
            conn.commit()
            user = User(row["id"], row["username"], row["password_hash"])
        except psycopg2.IntegrityError:
            conn.rollback()
            user = None
        finally:
            cur.close()
            conn.close()
        return user


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


def load_user_memories(user_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT key, value FROM user_memory WHERE user_id = %s", (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return ""
    return "Known facts about the user: " + "; ".join(
        [f"{r['key']}: {r['value']}" for r in rows]
    )


def extract_and_store_memory(user_id, conversation_text):
    client = get_client()
    if not client:
        return
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Extract personal facts about the user from this conversation. "
                 "Return ONLY a JSON object with key-value pairs. "
                 "Example: {\"name\": \"Alice\", \"likes\": \"Python\"}. "
                 "Return {} if no facts found."},
                {"role": "user", "content": f"Conversation:\n{conversation_text}\n\nExtract facts:"}
            ],
            max_tokens=200,
            temperature=0.1,
        )
        import json
        facts = json.loads(resp.choices[0].message.content.strip())
        if facts:
            conn = get_db()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            for key, value in facts.items():
                cur.execute(
                    "INSERT INTO user_memory (user_id, key, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                    (user_id, key, str(value))
                )
            conn.commit()
            cur.close()
            conn.close()
    except Exception:
        pass


MODEL = "openai/gpt-oss-120b"


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
            return jsonify({"response": "Error: GROQ_API_KEY not set. Please add it in Render settings."})

        data = request.json
        prompt = data.get("prompt", "")
        max_tokens = data.get("max_tokens", 1024)
        use_web_search = data.get("use_web_search", False)
        chat_id = data.get("chat_id")

        system_content = "You are a helpful assistant."

        memories = load_user_memories(current_user.id)
        if memories:
            system_content += f"\n\n{memories}"

        if use_web_search:
            tavily = get_tavily_client()
            if not tavily:
                return jsonify({"response": "Error: TAVILY_API_KEY not set. Please add it in Render settings."})
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
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT role, content FROM messages WHERE chat_id = %s ORDER BY created_at ASC",
                (chat_id,)
            )
            for row in cur.fetchall():
                messages.append({"role": row["role"], "content": row["content"]})
            cur.close()
            conn.close()

        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            top_p=0.9,
        )

        reply = response.choices[0].message.content

        if chat_id:
            conversation = "\n".join(
                [f"{m['role']}: {m['content']}" for m in messages[1:]]
            ) + f"\nassistant: {reply}"
            threading.Thread(target=extract_and_store_memory, args=(current_user.id, conversation)).start()

        return jsonify({"response": reply})

    except groq.AuthenticationError:
        return jsonify({"response": "Error: Invalid API key. Please check your Groq API key."})
    except groq.APIError as e:
        return jsonify({"response": f"Error: Groq API error - {str(e)}"})
    except Exception as e:
        return jsonify({"response": f"Error: {str(e)}"})


@app.route("/chats", methods=["GET"])
@login_required
def get_chats():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, title, created_at FROM chats WHERE user_id = %s ORDER BY created_at DESC",
        (current_user.id,)
    )
    chats = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{"id": c["id"], "title": c["title"], "created_at": c["created_at"].isoformat()} for c in chats])


@app.route("/chats", methods=["POST"])
@login_required
def create_chat():
    data = request.json or {}
    title = data.get("title", "New chat")
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "INSERT INTO chats (user_id, title) VALUES (%s, %s) RETURNING id, title, created_at",
        (current_user.id, title)
    )
    chat = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"id": chat["id"], "title": chat["title"], "created_at": chat["created_at"].isoformat()})


@app.route("/chats/<int:chat_id>", methods=["PUT"])
@login_required
def update_chat(chat_id):
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chats SET title = %s WHERE id = %s AND user_id = %s",
        (data["title"], chat_id, current_user.id)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/chats/<int:chat_id>", methods=["DELETE"])
@login_required
def delete_chat(chat_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM chats WHERE id = %s AND user_id = %s", (chat_id, current_user.id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/chats/<int:chat_id>/messages", methods=["GET"])
@login_required
def get_messages(chat_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT role, content FROM messages WHERE chat_id = %s ORDER BY created_at ASC",
        (chat_id,)
    )
    messages = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([{"role": m["role"], "content": m["content"]} for m in messages])


@app.route("/chats/<int:chat_id>/messages", methods=["POST"])
@login_required
def save_message(chat_id):
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)",
        (chat_id, data["role"], data["content"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
