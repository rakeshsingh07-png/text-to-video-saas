import os
import re
import secrets
import requests
import subprocess
from urllib.parse import quote_plus
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
import razorpay

# --- Load Environment Variables ---
load_dotenv()

raw_password = os.getenv("DB_PASSWORD", "Rak@jan940940")
safe_password = quote_plus(raw_password)
DB_URL = f"postgresql+psycopg2://postgres:{safe_password}@localhost:5432/videodb"
engine = create_engine(DB_URL)

# Ensure license table exists
with engine.begin() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS licenses (
            license_key VARCHAR(64) PRIMARY KEY,
            plan_name VARCHAR(50),
            credits_granted INT,
            is_used BOOLEAN DEFAULT FALSE,
            activated_by UUID REFERENCES users(user_id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))

# Third-Party Keys
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "58338490-5961215e8f19194db51131039")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_live_Tuq9tSa5YrP9fE")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "b32F0RGv7MGB1nC1sS7x9eqq")

rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="Text-to-Video Engine SaaS")

# --- Content Moderation & Legal Safety Filter ---
BLOCKED_PATTERNS = [
    r"\b(porn|pornography|nude|nudity|sex|erotic|nsfw|xxx)\b",
    r"\b(kill|murder|assassinate|suicide|terror|terrorist|bomb|explosive)\b",
    r"\b(rape|molest|pedophile|child\s*abuse|harass|blackmail)\b",
    r"\b(drug|cocaine|heroin|meth|weed|narcotic)\b",
    r"\b(hack|scam|fraud|phishing|stolen|bypass)\b",
    r"\b(hate\s*speech|slur|genocide|extremist)\b",
]

def sanitize_and_validate_prompt(prompt: str) -> str:
    """Validates that input strictly complies with safety and legal ethics."""
    clean_text = prompt.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    
    if len(clean_text) > 400:
        raise HTTPException(status_code=400, detail="Prompt exceeds maximum length of 400 characters.")
    
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, clean_text, re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail="[Content Moderation Alert] Prompt blocked. Content containing explicit, violent, abusive, defamatory, or unlawful themes violates our Acceptable Use Policy."
            )
    
    return clean_text

class GenerateRequest(BaseModel):
    prompt: str
    license_key: Optional[str] = None
    user_id: Optional[str] = None

class CreateOrderRequest(BaseModel):
    user_id: str
    plan: str

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    user_id: str
    plan: str

job_tracker = {}

def generate_voiceover(script_text, audio_output="voice.wav"):
    clean_text = script_text.replace('"', '').replace("'", "")
    abs_audio = os.path.abspath(audio_output)
    ps_cmd = (
        f'Add-Type -AssemblyName System.Speech; '
        f'$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
        f'$synth.SetOutputToWaveFile("{abs_audio}"); '
        f'$synth.Speak("{clean_text}"); '
        f'$synth.Dispose()'
    )
    subprocess.run(["powershell", "-Command", ps_cmd], check=True)
    return abs_audio

def download_pixabay_video(query, video_output="raw_broll.mp4"):
    clean_q = "".join(ch for ch in query if ch.isalnum() or ch.isspace()).strip()
    words = clean_q.split()
    
    candidates = [clean_q]
    if len(words) > 1:
        candidates.extend(words)
    candidates.extend(["technology", "nature", "success", "business"])

    hits = []
    for term in candidates:
        if not term or len(term) < 3:
            continue
        url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={term}&per_page=3"
        try:
            res = requests.get(url, timeout=5).json()
            hits = res.get('hits', [])
            if hits:
                break
        except Exception:
            continue

    if not hits:
        url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&per_page=3"
        hits = requests.get(url, timeout=5).json().get('hits', [])

    videos_meta = hits[0]['videos']
    selected_clip = videos_meta.get('tiny') or videos_meta.get('small') or videos_meta.get('medium')
    download_url = selected_clip['url']

    r = requests.get(download_url, stream=True, timeout=10)
    with open(video_output, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024 * 512):
            if chunk:
                f.write(chunk)
    return os.path.abspath(video_output)

def assemble_video(video_file, audio_file, output_file, caption_text, is_demo=False):
    clean_caption = caption_text.replace(":", "\\:").replace("'", "").replace('"', '')
    font_path = "C\\:/Windows/Fonts/arial.ttf"
    
    caption_filter = (
        f"drawtext=fontfile='{font_path}':text='{clean_caption}':"
        f"fontcolor=white:fontsize=26:box=1:boxcolor=black@0.6:boxborderw=8:"
        f"x=(w-text_w)/2:y=h-text_h-40"
    )
    
    if is_demo:
        filter_str = f"{caption_filter},drawtext=fontfile='{font_path}':text='FREE DEMO PREVIEW':fontcolor=yellow@0.8:fontsize=32:x=(w-text_w)/2:y=40"
        duration_arg = ["-t", "3"]
    else:
        filter_str = caption_filter
        duration_arg = ["-shortest"]

    cmd = [
        "ffmpeg", "-y",
        "-i", video_file,
        "-i", audio_file,
        "-vf", filter_str,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-threads", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        *duration_arg,
        "-pix_fmt", "yuv420p",
        output_file
    ]
    subprocess.run(cmd, check=True)
    return output_file

def process_job(user_id: Optional[str], prompt: str, job_id: str, is_demo: bool):
    try:
        print(f"\n--- [START] Rendering Job: {job_id} ---")
        audio_path = generate_voiceover(prompt, f"voice_{job_id}.wav")
        raw_video = download_pixabay_video(prompt, f"broll_{job_id}.mp4")
        output_filename = f"video_{job_id[:8]}.mp4"
        assemble_video(raw_video, audio_path, output_filename, prompt, is_demo=is_demo)

        job_tracker[job_id] = {"status": "COMPLETED", "path": output_filename}

        if user_id:
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO video_jobs (user_id, prompt, status, video_path) VALUES (:u_id, :prompt, 'COMPLETED', :path)"),
                    {"u_id": user_id, "prompt": prompt, "path": output_filename}
                )

        if os.path.exists(audio_path): 
            os.remove(audio_path)
        if os.path.exists(raw_video): 
            os.remove(raw_video)
        print(f"--- [DONE] Video Ready: {output_filename} ---\n")
    except Exception as e:
        print(f"[ERROR in Job {job_id}] {e}")
        job_tracker[job_id] = {"status": "FAILED", "path": None}
        if user_id:
            with engine.begin() as conn:
                conn.execute(text("UPDATE users SET credits = credits + 1 WHERE user_id = :u_id"), {"u_id": user_id})

@app.get("/", response_class=HTMLResponse)
def index_page():
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>AI Video Generator & Payment Gateway</title>
    <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 30px; line-height: 1.5; }}
        .container {{ max-width: 820px; margin: auto; }}
        .card {{ background: #1e293b; padding: 25px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); margin-bottom: 25px; }}
        h2, h3 {{ margin-top: 0; color: #38bdf8; }}
        label {{ font-size: 13px; font-weight: 600; color: #94a3b8; display: block; margin-top: 12px; }}
        input, textarea {{ width: 100%; padding: 10px; margin-top: 5px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }}
        button {{ width: 100%; margin-top: 15px; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; }}
        button:hover {{ background: #0369a1; }}
        .pricing-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; margin-top: 15px; }}
        .plan-box {{ background: #0f172a; border: 2px solid #334155; border-radius: 8px; padding: 15px; text-align: center; cursor: pointer; }}
        .plan-box.selected {{ border-color: #38bdf8; background: #1e3a5f; }}
        .badge {{ display: inline-block; padding: 3px 8px; font-size: 11px; background: #38bdf8; color: #000; border-radius: 4px; font-weight: bold; }}
        #status, #payStatus {{ margin-top: 15px; padding: 12px; border-radius: 6px; display: none; }}
        .success {{ background: #064e3b; color: #6ee7b7; border: 1px solid #059669; }}
        .error {{ background: #7f1d1d; color: #fca5a5; border: 1px solid #dc2626; }}
        video {{ width: 100%; border-radius: 8px; margin-top: 15px; }}
        
        .disclaimer-card {{
            background: #111827;
            border: 1px solid #374151;
            padding: 20px;
            border-radius: 8px;
            font-size: 12px;
            color: #9ca3af;
            line-height: 1.6;
        }}
        .disclaimer-card h4 {{
            margin: 0 0 8px 0;
            color: #e5e7eb;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .disclaimer-list {{
            margin: 8px 0 0 16px;
            padding: 0;
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h2>Text-to-Video SaaS Engine</h2>
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span id="quotaInfo" style="font-size:13px; color:#cbd5e1;">Free Demos Remaining: <strong>3/3</strong></span>
            <span style="font-size:12px; color:#10b981; font-weight:600;">✓ Automated Safety Guardrails Active</span>
        </div>

        <label>Video Script / Prompt</label>
        <textarea id="prompt" rows="3" placeholder="Enter ethical video script or concept...">Deepak I made it now anyone can use it</textarea>
        
        <label>License Key (Optional - Leave blank for Free Demo)</label>
        <input type="text" id="licenseKey" value="E68D-AB69-E521-C669" />

        <button id="genBtn" onclick="submitVideo()">Generate Video</button>
        <div id="status"></div>
        <div id="videoBox" style="display:none; text-align:center;">
            <h4 style="color:#38bdf8; margin: 10px 0 0 0;">Generated Output</h4>
            <video id="player" controls autoplay></video>
        </div>
    </div>

    <div class="card">
        <h3>Purchase License & Top-Up Credits</h3>
        <p style="color:#94a3b8; font-size:14px; margin-top:0;">Direct settlement via UPI, Card, or Net Banking into your bank account.</p>
        
        <label>Subscriber User ID</label>
        <input type="text" id="payUserId" value="b59fa494-2256-4625-8ece-d8884d89502f" />

        <div class="pricing-grid">
            <div id="planTest" class="plan-box selected" onclick="selectPlan('test')">
                <span class="badge" style="background:#10b981; color:#fff;">TEST PLAN</span>
                <h4 style="margin:10px 0;">5 Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹10</p>
            </div>
            <div id="planStarter" class="plan-box" onclick="selectPlan('starter')">
                <span class="badge">STARTER</span>
                <h4 style="margin:10px 0;">50 Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹499</p>
            </div>
            <div id="planPro" class="plan-box" onclick="selectPlan('pro')">
                <span class="badge">PRO VALUE</span>
                <h4 style="margin:10px 0;">200 Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹1,499</p>
            </div>
        </div>

        <button style="background:#10b981;" onclick="payWithRazorpay()">Pay Securely via UPI / Card</button>
        <div id="payStatus"></div>
    </div>

    <div class="disclaimer-card">
        <h4>Legal Compliance, Acceptable Use & Liability Disclaimer</h4>
        <p style="margin:0 0 8px 0;">
            This software functions strictly as an automated processing utility. By submitting prompts or generating media, you acknowledge and agree to the following statutory conditions:
        </p>
        <ul class="disclaimer-list">
            <li><strong>Automated Ethical Filtering:</strong> The system automatically blocks content involving defamation, nudity/sexual exploitation, violence, illegal substances, hate speech, national security threats, copyright infringements, or cyber-harassment.</li>
            <li><strong>User Liability:</strong> The end-user retains full legal accountability for all submitted text prompts and downstream distribution of generated video/audio files under applicable Information Technology and telecommunication statutes.</li>
            <li><strong>Platform Indemnification:</strong> The platform owners, service operators, and infrastructure providers are held entirely harmless and exempt from civil or criminal liability resulting from willful misuse, third-party intellectual property breaches, or deceptive claims generated by users.</li>
        </ul>
    </div>
</div>

<script>
    let selectedPlan = 'test';
    let demoCount = 0;
    const rzpKeyId = "{RAZORPAY_KEY_ID}";

    function selectPlan(plan) {{
        selectedPlan = plan;
        document.getElementById('planTest').classList.toggle('selected', plan === 'test');
        document.getElementById('planStarter').classList.toggle('selected', plan === 'starter');
        document.getElementById('planPro').classList.toggle('selected', plan === 'pro');
    }}

    async function payWithRazorpay() {{
        const payStatus = document.getElementById('payStatus');
        payStatus.style.display = 'block';
        payStatus.className = '';
        payStatus.innerText = 'Creating payment order...';

        const userId = document.getElementById('payUserId').value;
        const res = await fetch('/create-order', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ user_id: userId, plan: selectedPlan }})
        }});
        const orderData = await res.json();
        if (!res.ok) {{
            payStatus.className = 'error';
            payStatus.innerText = orderData.detail || 'Could not initiate payment.';
            return;
        }}

        const options = {{
            "key": rzpKeyId,
            "amount": orderData.amount,
            "currency": "INR",
            "name": "Text-to-Video Engine",
            "description": selectedPlan.toUpperCase() + " Plan License",
            "order_id": orderData.order_id,
            "handler": async function (response) {{
                payStatus.innerText = 'Verifying payment with bank...';
                const verifyRes = await fetch('/verify-payment', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        razorpay_order_id: response.razorpay_order_id,
                        razorpay_payment_id: response.razorpay_payment_id,
                        razorpay_signature: response.razorpay_signature,
                        user_id: userId,
                        plan: selectedPlan
                    }})
                }});
                const verifyData = await verifyRes.json();
                if (verifyRes.ok) {{
                    payStatus.className = 'success';
                    payStatus.innerHTML = `<strong>Payment Received!</strong><br>License Key: <code style="color:#38bdf8; font-weight:bold;">${{verifyData.license_key}}</code><br>Credits Added: +${{verifyData.credits}}<br><em>Key automatically filled into the generator!</em>`;
                    document.getElementById('licenseKey').value = verifyData.license_key;
                }} else {{
                    payStatus.className = 'error';
                    payStatus.innerText = verifyData.detail || 'Payment verification failed.';
                }}
            }},
            "theme": {{ "color": "#0284c7" }}
        }};
        const rzp = new Razorpay(options);
        rzp.open();
    }}

    async function submitVideo() {{
        const status = document.getElementById('status');
        const videoBox = document.getElementById('videoBox');
        const btn = document.getElementById('genBtn');
        const license = document.getElementById('licenseKey').value.trim();
        const prompt = document.getElementById('prompt').value.trim();

        if (!prompt) return alert('Please enter a video prompt');

        status.style.display = 'block';
        status.className = '';
        status.innerText = 'Checking content safety & submitting job...';
        videoBox.style.display = 'none';
        btn.disabled = true;

        try {{
            const res = await fetch('/generate', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    prompt: prompt,
                    license_key: license || null,
                    user_id: document.getElementById('payUserId').value
                }})
            }});
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail);

            if (data.is_demo) {{
                demoCount++;
                document.getElementById('quotaInfo').innerHTML = `Free Demos Remaining: <strong>${{3 - demoCount}}/3</strong>`;
            }}

            status.className = 'success';
            status.innerHTML = `<strong>${{data.is_demo ? 'Rendering 3s Demo Clip' : 'Rendering Full Video'}}...</strong><br>Job ID: ${{data.job_id}}`;
            poll(data.job_id);
        }} catch (err) {{
            status.className = 'error';
            status.innerText = err.message;
            btn.disabled = false;
        }}
    }}

    function poll(jobId) {{
        const timer = setInterval(async () => {{
            try {{
                const res = await fetch(`/status/${{jobId}}`);
                const data = await res.json();
                if (data.status === 'COMPLETED') {{
                    clearInterval(timer);
                    document.getElementById('status').innerHTML = '<strong>Render Complete!</strong>';
                    document.getElementById('player').src = `/download/${{data.video_path}}`;
                    document.getElementById('videoBox').style.display = 'block';
                    document.getElementById('genBtn').disabled = false;
                }} else if (data.status === 'FAILED') {{
                    clearInterval(timer);
                    document.getElementById('status').className = 'error';
                    document.getElementById('status').innerText = 'Generation encountered an error.';
                    document.getElementById('genBtn').disabled = false;
                }}
            }} catch (err) {{
                console.error("Polling error:", err);
            }}
        }}, 2000);
    }}
</script>
</body>
</html>"""

@app.post("/create-order")
def create_order(req: CreateOrderRequest):
    plan_prices = {"test": 1000, "starter": 49900, "pro": 149900}
    amount = plan_prices.get(req.plan.lower(), 1000)

    try:
        order = rzp_client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1
        })
        return {"order_id": order["id"], "amount": amount}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gateway Error: {str(e)}")

@app.post("/verify-payment")
def verify_payment(req: VerifyPaymentRequest):
    try:
        rzp_client.utility.verify_payment_signature({
            'razorpay_order_id': req.razorpay_order_id,
            'razorpay_payment_id': req.razorpay_payment_id,
            'razorpay_signature': req.razorpay_signature
        })
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payment verification signature.")

    credits_map = {"test": 5, "starter": 50, "pro": 200}
    credits = credits_map.get(req.plan.lower(), 5)
    raw_token = secrets.token_hex(8).upper()
    license_key = f"{raw_token[:4]}-{raw_token[4:8]}-{raw_token[8:12]}-{raw_token[12:]}"

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO licenses (license_key, plan_name, credits_granted, is_used, activated_by)
                VALUES (:k, :p, :c, TRUE, :u)
            """),
            {"k": license_key, "p": req.plan, "c": credits, "u": req.user_id}
        )
        conn.execute(
            text("UPDATE users SET credits = credits + :c WHERE user_id = :u"),
            {"c": credits, "u": req.user_id}
        )

    return {"status": "success", "license_key": license_key, "credits": credits}

@app.post("/generate")
def generate_endpoint(req: GenerateRequest, background_tasks: BackgroundTasks):
    validated_prompt = sanitize_and_validate_prompt(req.prompt)

    job_id = secrets.token_hex(8)
    job_tracker[job_id] = {"status": "PROCESSING", "path": None}
    
    # Paid License Path
    if req.license_key:
        with engine.begin() as conn:
            user = conn.execute(
                text("SELECT credits FROM users WHERE user_id = :u_id FOR UPDATE"),
                {"u_id": req.user_id}
            ).fetchone()
            if not user or user[0] <= 0:
                raise HTTPException(status_code=402, detail="Credits exhausted. Please purchase a top-up.")
            
            conn.execute(
                text("UPDATE users SET credits = credits - 1 WHERE user_id = :u_id"),
                {"u_id": req.user_id}
            )
        
        background_tasks.add_task(process_job, req.user_id, validated_prompt, job_id, is_demo=False)
        return {"job_id": job_id, "is_demo": False}

    # Free Demo Path
    background_tasks.add_task(process_job, None, validated_prompt, job_id, is_demo=True)
    return {"job_id": job_id, "is_demo": True}

@app.get("/status/{job_id}")
def check_status(job_id: str):
    info = job_tracker.get(job_id)
    if info:
        return {"status": info["status"], "video_path": info["path"]}
    
    candidate_filename = f"video_{job_id[:8]}.mp4"
    if os.path.exists(candidate_filename):
        return {"status": "COMPLETED", "video_path": candidate_filename}
        
    return {"status": "PROCESSING", "video_path": None}

@app.get("/download/{filename}")
def download(filename: str):
    if os.path.exists(filename):
        return FileResponse(filename, media_type="video/mp4", filename=filename)
    raise HTTPException(status_code=404, detail="File not found")
