from flask import Flask, jsonify, request, render_template, redirect, url_for, flash, send_file, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from pyngrok import ngrok

import qrcode

import uuid
import os
import io
import base64
import secrets
from pathlib import Path
import shutil

from datetime import datetime, timedelta


#Config
UPLOAD_FOLDER = 'static/uploads/'
ALLOWED_EXTENSIONS = {'.pdf'}

app = Flask(__name__)
app.secret_key = secrets.token_urlsafe(24)

#Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


#Database model
class Room(db.Model):
    id = db.Column(db.String, primary_key=True)
    admin_uuid = db.Column(db.String, nullable=False)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)

################    State dict

# Per-user state should live in Flask's `session` (cookie-backed). Use
# session.setdefault("key", default) when initializing defaults in routes.

# Server-wide/public values (the ngrok public URL) are stored separately.
SERVER_PUBLIC_URL = None

# token_store maps upload tokens (the token in the /upload/<token> URL)
# to metadata about the uploaded file. This is server-side so upload
# requests and admin sessions can communicate.
token_store = {}


#Utility functions
def allowed_file(filename):
    return Path(filename).suffix in ALLOWED_EXTENSIONS

def generate_qr_data_url(text: str) -> str:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'

def send_to_printer(file_path: str):
    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError("File not found for printing.")

    if os.name == 'nt':
        # Windows: use default associated application's print verb
        try:
            # This returns immediately in many cases while the app handles the job.
            os.startfile(str(file_path), "print")
            return True
        except Exception as e:
            raise RuntimeError(f"Windows printing failed: {e}")
    else:
        # POSIX (Linux/macOS): try lp then lpr
        last_exc = None
        for cmd in (["lp", file_path], ["lpr", file_path]):
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return True
            except FileNotFoundError:
                last_exc = FileNotFoundError("lp/lpr command not found")
                continue
            except subprocess.CalledProcessError as e:
                last_exc = e
        raise RuntimeError(f"Printing failed or no print command available: {last_exc}")



@app.route('/print_and_confirm/<token>', methods=['POST'])
def print_and_confirm(token):
    # ensure per-user keys exist
    session.setdefault('session_token', None)
    session.setdefault('upload_name', None)
    session.setdefault('file_name', None)

    if session.get('session_token') != token:
        flash("Invalid/expired session token.", "error")
        return redirect(url_for('admin'))

    p = os.path.join(UPLOAD_FOLDER, session.get('session_token') or '', session.get('file_name') or '')
    if not p or not os.path.exists(p):
        flash("No file uploaded to print.", "error")
        return redirect(url_for('admin'))

    try:
        send_to_printer(p)
    except Exception as e:
        flash(f"Printing failed: {e}", "error")
        return redirect(url_for('admin'))

    # If we reached here, printing was started successfully.
    # Remove entire upload folder for this token and clean token_store.
    cleanup_entire_upload_folder(Path(UPLOAD_FOLDER) / token)
    token_store.pop(token, None)
    # if the current admin session owned this token, clear their session keys
    if session.get('session_token') == token:
        session['upload_name'] = None
        session['file_name'] = None
        session['session_token'] = None

    return render_template('printed.html')


def cleanup_uploaded_file():
    session.setdefault('session_token', None)
    session.setdefault('file_name', None)
    token = session.get('session_token')
    if not token:
        return
    p = os.path.join(UPLOAD_FOLDER, token, session.get('file_name') or '')
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    


def cleanup_entire_upload_folder(path):
    try:
        if path.exists():
            shutil.rmtree(path)
    except Exception:
        pass


def stop_ngrok_and_shutdown():
    # disconnect & kill ngrok tunnel
    try:
        # per-user tunnel is not stored here; if stored in session it would be handled per-request
        # fall back to killing global ngrok process
        ngrok.kill()
    except Exception:
        pass

    # try to trigger Werkzeug shutdown via internal endpoint
    try:
        import requests
        try:
            requests.get("http://127.0.0.1:5000/_internal_shutdown_trigger", timeout=1)
        except Exception:
            pass
    except Exception:
        pass

    # fallback to process exit
    try:
        os._exit(0)
    except Exception:
        pass


#TODO: thread


@app.route('/file/<token>')
def serve_file(token):
    # Serve the uploaded PDF for the shopkeeper's browser
    # The token in the URL must match the admin's session_token if they are the admin
    # But uploads are stored under the token folder; just read the file from disk.
    p = os.path.join(UPLOAD_FOLDER, token, '')
    # find the first file in the folder if file name not provided
    try:
        # look for any file in the token directory
        files = os.listdir(os.path.join(UPLOAD_FOLDER, token))
    except Exception:
        return "No file uploaded", 404
    if not files:
        return "No file uploaded", 404
    file_path = os.path.join(UPLOAD_FOLDER, token, files[0])
    return send_file(file_path, mimetype='application/pdf', as_attachment=False,
                     download_name=os.path.basename(file_path))



@app.route('/print/<token>', methods=['GET'])
def print_view(token):
    # Show print view for a token if the upload exists
    token_dir = os.path.join(UPLOAD_FOLDER, token)
    if not os.path.exists(token_dir):
        return "Invalid token", 404
    files = os.listdir(token_dir)
    if not files:
        return "No uploaded file to print", 404
    file_url = url_for('serve_file', token=token)
    return render_template('print_view.html', file_url=file_url, token=token)


@app.route('/confirm_print/<token>/<is_admin>', methods=['POST'])
def confirm_print(token, is_admin):
    # Confirm print: validate token exists, remove upload folder
    token_dir = os.path.join(UPLOAD_FOLDER, token)
    if not os.path.exists(token_dir):
        return jsonify({"status": "error", "msg": "invalid token"}), 403
    cleanup_entire_upload_folder(Path(token_dir))
    token_store.pop(token, None)
    if session.get('session_token') == token:
        session['upload_name'] = None
        session['file_name'] = None
        session['session_token'] = None
    admin = is_admin
    # Redirect to stop_session with token so the server can stop the correct session
    return redirect(url_for('stop_session', token=token, is_admin=admin))

@app.route('/_internal_shutdown_trigger', methods=['GET'])
def _shutdown_trigger():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        return "No shutdown", 500
    func()
    return "Shutting down..."


########## Routes
@app.route('/')
def root():
    return render_template('index.html')



#For Printers
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    #Admin page for shopkeepers
    # Initialize per-user session defaults

    qr = None
    upload_link = None
    if session.get('session_token'):
        # Verify session token still exists in DB and the upload folder is present
        token = session.get('session_token')
        room = Room.query.filter_by(id=token).first()
        token_dir = os.path.join(UPLOAD_FOLDER, token)
        if not room or not os.path.exists(token_dir):
            # Session expired/removed (maybe uploader confirmed print) — clear and show invalid
            flash('This session has expired or been closed (invalid token).', 'error')
            session['session_token'] = None
            session['upload_name'] = None
            session['file_name'] = None
            return render_template('admin.html', active=False, uploaded=False, uploaded_name=None, qr=None, upload_link=None, session_token=None)

        public_url = SERVER_PUBLIC_URL
        if public_url:
            upload_link = f"{public_url}/upload/{token}"
            try:
                qr = generate_qr_data_url(upload_link)
            except Exception:
                qr = None

    return render_template(
        'admin.html',
        active=bool(session.get('session_token')),
        uploaded=bool(session.get('upload_name')),
        uploaded_name=session.get('upload_name'),
        qr=qr,
        upload_link=upload_link,
        session_token=session.get('session_token')
    )




@app.route('/admin_status', methods=['GET'])
def admin_status():
    #Check status and send back to html
    #polled by admin ui (in admin.html script) for auto-refresh
    
    # If the admin hasn't started a session, respond with inactive state
    session_token = session.get('session_token')
    if not session_token:
        return jsonify({
            "active": False,
            "uploaded": False,
            "uploaded_name": None,
            "upload_link": None,
            "qr": None
        })

    public_url = SERVER_PUBLIC_URL
    upload_link = None
    qr = None
    if session_token and public_url:
        upload_link = f"{public_url}/upload/{session_token}"
        try:
            qr = generate_qr_data_url(upload_link)
        except Exception:
            qr = None
            return "error generating qr code", 500

    # Check server-side token_store for upload metadata (uploader won't have admin cookie)
    uploaded = False
    uploaded_name = None
    meta = token_store.get(session_token)
    if meta:
        uploaded = True
        uploaded_name = meta.get('upload_name')

    return jsonify({
        "active": True,
        "uploaded": uploaded,
        "uploaded_name": uploaded_name,
        "upload_link": upload_link,
        "qr": qr
    })



#Start a temporary session
@app.route('/start_session', methods=['POST'])
def start_session():
    # Create a new session token and store it in the user's Flask session
    session.setdefault('session_token', None)
    if session.get('session_token'):
        flash("There is already an active session. Finish it first.", "error")
        return redirect(url_for('admin'))

    room = Room(id = str(uuid.uuid4()), admin_uuid = str(uuid.uuid4()), date_created = datetime.utcnow())
    db.session.add(room)
    db.session.commit()

    session['session_token'] = room.id
    session['started_at'] = datetime.utcnow().isoformat()
    session['upload_name'] = None
    session['file_name'] = None

    os.makedirs(os.path.join(UPLOAD_FOLDER, room.id), exist_ok=True)

    return redirect(url_for('admin'))


#When user uploads a file
@app.route('/upload/<token>', methods=['GET', 'POST'])
def upload(token):
    # public upload endpoint used by customer via ngrok url
    # validate token exists by checking folder
    token_dir = os.path.join(UPLOAD_FOLDER, token)
    if not os.path.exists(token_dir):
        return render_template('upload.html', error="This upload link is invalid or expired."), 404

    if request.method == 'GET':
        # check whether an upload exists in the token folder
        files = os.listdir(token_dir)
        if files:
            return render_template('upload.html', message="A file has already been uploaded for this session. The link will remain available until the shopkeeper prints it.", token=token), 200
        return render_template('upload.html', token=token)

    # POST -> handle file
    files = os.listdir(token_dir)
    if files:
        return render_template('upload.html', message="A file has already been uploaded for this session.", token=token), 400

    file = request.files.get('file')
    if not file:
        return render_template('upload.html', error="No file provided.", token=token), 400

    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        return render_template('upload.html', error="Only PDF files are allowed.", token=token), 400

    unique_name = f"{secrets.token_hex(12)}_{filename}"
    save_path = os.path.join(UPLOAD_FOLDER, token, unique_name)
    try:
        file.save(save_path)
    except Exception as e:
        return render_template('upload.html', error=f"Failed to save file: {e}", token=token), 500

    # store file metadata server-side so admin can access it
    token_store[token] = {
        'upload_name': filename,
        'file_name': unique_name,
        'uploaded_at': datetime.utcnow().isoformat()
    }
    # If the current user's session corresponds to the admin who created the token,
    # update their session values so admin_status and admin page reflect the upload.
    try:
        # there is no reliable cross-process way to find which admin created the token
        # because admin session data is cookie-bound; if the uploader is the admin
        # they'll have the same cookie and this will update their session. This
        # is primarily useful when admin uploads from same browser.
        if session.get('session_token') == token:
            session['upload_name'] = filename
            session['file_name'] = unique_name
    except Exception:
        pass
    flash("File uploaded successfully. The shopkeeper has been notified in their local UI.", "success")
    return render_template('upload.html', message="Upload successful. The shopkeeper will receive the file shortly.", token=token)

#Stop session and delete room from database, and delete uploaded files
@app.route('/stop_session/<is_admin>', methods=['GET', 'POST'])
@app.route('/stop_session/<token>/<is_admin>', methods=['GET', 'POST'])
def stop_session(is_admin, token=None):
    """Stop the current session, remove DB room if present, clear state and redirect home.

    Allowing GET here avoids a Method Not Allowed when other handlers redirect to this
    endpoint after performing POST work.
    """
    # Determine which token to stop: prefer explicit token argument (from uploader confirm)
    session_token = token or session.get('session_token')
    room = None
    if session_token:
        room = Room.query.filter_by(id=session_token).first()

    if not room:
        flash("No session found", "error")
    else:
        try:
            db.session.delete(room)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Failed to remove session from database.", "error")

    # cleanup upload folder for this token
    try:
        if session_token:
            cleanup_entire_upload_folder(Path(UPLOAD_FOLDER) / session_token)
            token_store.pop(session_token, None)
    except Exception:
        pass

    # If the user's flask session owned this token, clear their session keys
    if session.get('session_token') == session_token:
        session['session_token'] = None
        session['upload_name'] = None
        session['started_at'] = None

    if str(is_admin) == '1':
        return redirect(url_for('root'))
    else:
        return render_template('printed.html')

if __name__ == '__main__':
    with app.app_context():   # Create database tables if they don't exist
        db.create_all()
    
    tunnel = ngrok.connect(5000)
    public_url = tunnel.public_url
    print(f"public url: {public_url}")
 
 
    try:
        app.run(debug=True, port=5000)
    finally:
        try:
            ngrok.disconnect(tunnel.public_ur)
        except Exception:
            pass
        ngrok.kill()
 
    #SERVER_PUBLIC_URL = ngrok.connect(5000).public_url
    #print(f"public url: {SERVER_PUBLIC_URL}")
    #app.run(debug=True, port=5000)


#TODO: change uploads folder to tmp    