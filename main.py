import os
import re
import secrets
import requests
import subprocess
from urllib.parse import quote_plus
from typing import Optional, List
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from gtts import gTTS
import razorpay
import replicate

load_dotenv()

raw_password = os.getenv("DB_PASSWORD", "Rak@jan940940")
safe_password = quote_plus(raw_password)
DB_URL = f"postgresql+psycopg2://postgres:{safe_password}@localhost:5432/videodb"
engine = create_engine(DB_URL)

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

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "58338490-5961215e8f19194db51131039")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_live_Tuq9tSa5YrP9fE")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "b32F0RGv7MGB1nC1sS7x9eqq")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")

rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="Global Text-to-Video Creator Studio")

AVATAR_PRESETS = {
    "devotional": {
        "name": "Aarav - Cultural & Spiritual",
        "url": "https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?auto=format&fit=crop&w=400&q=80"
    },
    "business": {
        "name": "Sarah - Executive Anchor",
        "url": "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?auto=format&fit=crop&w=400&q=80"
    },
    "tech": {
        "name": "Alex - Tech Specialist",
        "url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=400&q=80"
    },
    "creative": {
        "name": "Leo - Creator Host",
        "url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=400&q=80"
    }
}

BLOCKED_PATTERNS = [
    r"\b(porn|pornography|nude|nudity|sex|erotic|nsfw|xxx)\b",
    r"\b(kill|murder|assassinate|suicide|terror|terrorist|bomb|explosive)\b",
    r"\b(rape|molest|pedophile|child\s*abuse|harass|blackmail)\b",
    r"\b(drug|cocaine|heroin|meth|weed|narcotic)\b",
    r"\b(hack|scam|fraud|phishing|stolen|bypass)\b",
    r"\b(hate\s*speech|slur|genocide|extremist)\b",
]

def sanitize_and_validate_prompt(prompt: str) -> str:
    clean_text = prompt.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Script/prompt cannot be empty.")
    if len(clean_text) > 2500:
        raise HTTPException(status_code=400, detail="Script exceeds 2,500 characters limit.")
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, clean_text, re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail="[Content Moderation Alert] Prompt blocked under international safety terms."
            )
    return clean_text

class GenerateRequest(BaseModel):
    prompt: str
    language: Optional[str] = "en"
    character: Optional[str] = "devotional"
    engine_mode: Optional[str] = "stock"  # "generative" or "stock"
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

def ensure_avatar_asset(avatar_key: str) -> str:
    key = avatar_key.lower() if avatar_key in AVATAR_PRESETS else "devotional"
    local_path = os.path.abspath(f"avatar_{key}.jpg")
    
    if not os.path.exists(local_path) or os.path.getsize(local_path) < 2000:
        url = AVATAR_PRESETS[key]["url"]
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200 and len(r.content) > 2000:
                with open(local_path, "wb") as f:
                    f.write(r.content)
        except Exception:
            pass

    if not os.path.exists(local_path) or os.path.getsize(local_path) < 2000:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x0284c7:s=220x220:d=1",
            "-frames:v", "1", local_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    return local_path

def generate_voiceover(script_text: str, audio_output: str, lang: str = "en") -> str:
    abs_audio = os.path.abspath(audio_output)
    clean_text = script_text.replace('"', '').replace("'", "").strip()
    
    try:
        tts = gTTS(text=clean_text, lang=lang, slow=False)
        temp_mp3 = abs_audio + ".mp3"
        tts.save(temp_mp3)
        cmd = ["ffmpeg", "-y", "-i", temp_mp3, "-acodec", "pcm_s16le", "-ar", "24000", abs_audio]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(temp_mp3):
            os.remove(temp_mp3)
        return abs_audio
    except Exception as e:
        ps_cmd = (
            f'Add-Type -AssemblyName System.Speech; '
            f'$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
            f'$synth.SetOutputToWaveFile("{abs_audio}"); '
            f'$synth.Speak("{clean_text}"); '
            f'$synth.Dispose()'
        )
        subprocess.run(["powershell", "-Command", ps_cmd], check=True)
        return abs_audio

def generate_ai_video_replicate(prompt: str, video_output: str) -> str:
    """Invokes generative AI video models via Replicate when enabled."""
    if not REPLICATE_API_TOKEN:
        return download_pixabay_video(prompt, video_output)
    
    try:
        print(f"[REPLICATE AI] Synthesizing generative video for: {prompt[:60]}...")
        output = replicate.run(
            "minimax/video-01",
            input={"prompt": prompt, "prompt_optimizer": True}
        )
        video_url = str(output)
        r = requests.get(video_url, stream=True, timeout=20)
        with open(video_output, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
        return os.path.abspath(video_output)
    except Exception as e:
        print(f"[Replicate Fallback to Pixabay] {e}")
        return download_pixabay_video(prompt, video_output)

def download_pixabay_video(query: str, video_output: str) -> str:
    has_non_ascii = any(ord(c) > 127 for c in query)
    if has_non_ascii:
        search_terms = ["sunrise", "ocean", "temple", "nature"]
    else:
        clean_q = "".join(ch for ch in query if ch.isalnum() or ch.isspace()).strip()
        search_terms = [clean_q, "cinematic ocean", "divine light", "nature"]

    hits = []
    for term in search_terms:
        if not term or len(term) < 3:
            continue
        url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={quote_plus(term)}&per_page=3"
        try:
            res = requests.get(url, timeout=6).json()
            hits = res.get('hits', [])
            if hits:
                break
        except Exception:
            continue

    if not hits:
        url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q=ocean&per_page=3"
        hits = requests.get(url, timeout=6).json().get('hits', [])

    selected_clip = hits[0]['videos'].get('small') or hits[0]['videos'].get('tiny') or hits[0]['videos'].get('medium')
    download_url = selected_clip['url']

    r = requests.get(download_url, stream=True, timeout=10)
    with open(video_output, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024 * 512):
            if chunk:
                f.write(chunk)
    return os.path.abspath(video_output)

def render_scene(broll_file: str, audio_file: str, avatar_file: str, output_file: str, caption_text: str, is_demo: bool = False) -> str:
    caption_txt_file = os.path.abspath(output_file + "_cap.txt")
    with open(caption_txt_file, "w", encoding="utf-8") as f:
        f.write(caption_text.strip())

    safe_txt_path = caption_txt_file.replace("\\", "/").replace(":", "\\:")

    possible_fonts = [
        "C\\:/Windows/Fonts/Nirmala.ttf",
        "C\\:/Windows/Fonts/mangal.ttf",
        "C\\:/Windows/Fonts/arial.ttf"
    ]
    font_path = possible_fonts[0]
    for pf in possible_fonts:
        if os.path.exists(pf.replace("\\:", ":")):
            font_path = pf
            break

    filter_complex = (
        f"[0:v]scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720[bg]; "
        f"[1:v]scale=200:200[avatar]; "
        f"[bg][avatar]overlay=W-w-30:H-h-30[vpip]; "
        f"[vpip]drawtext=fontfile='{font_path}':textfile='{safe_txt_path}':"
        f"fontcolor=white:fontsize=22:box=1:boxcolor=black@0.65:boxborderw=6:"
        f"x=(w-text_w)/2:y=h-text_h-25,"
        f"drawtext=fontfile='{font_path}':text='[AI SYNTHETIC MEDIA - DISCLOSED]':fontcolor=white@0.6:fontsize=12:x=20:y=20"
    )

    if is_demo:
        filter_complex += f",drawtext=fontfile='{font_path}':text='FREE DEMO PREVIEW':fontcolor=yellow@0.9:fontsize=32:x=(w-text_w)/2:y=50[vout]"
        duration_arg = ["-t", "6"]
    else:
        filter_complex += "[vout]"
        duration_arg = ["-shortest"]

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", broll_file,
        "-loop", "1", "-i", avatar_file,
        "-i", audio_file,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "2:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-threads", "0",
        "-c:a", "aac",
        "-b:a", "192k",
        *duration_arg,
        "-pix_fmt", "yuv420p",
        output_file
    ]
    subprocess.run(cmd, check=True)

    if os.path.exists(caption_txt_file):
        try: os.remove(caption_txt_file)
        except Exception: pass

    return output_file

def split_script_into_scenes(script: str) -> List[str]:
    raw_sentences = re.split(r'(?<=[.!?|।])\s+', script.strip())
    scenes = [s.strip() for s in raw_sentences if len(s.strip()) > 3]
    return scenes if scenes else [script.strip()]

def process_job(user_id: Optional[str], prompt: str, character_choice: str, language_choice: str, engine_mode: str, job_id: str, is_demo: bool):
    temp_files = []
    try:
        print(f"\n--- [START] Rendering ({engine_mode.upper()}) Job: {job_id} ---")
        scenes = split_script_into_scenes(prompt)
        avatar_path = ensure_avatar_asset(character_choice)
        
        render_scenes = scenes[:1] if is_demo else scenes
        scene_video_paths = []

        for idx, scene_text in enumerate(render_scenes):
            s_audio = f"temp_aud_{job_id}_{idx}.wav"
            s_broll = f"temp_broll_{job_id}_{idx}.mp4"
            s_scene_out = f"scene_{job_id}_{idx}.mp4"
            temp_files.extend([s_audio, s_broll])

            generate_voiceover(scene_text, s_audio, lang=language_choice)

            if engine_mode == "generative" and not is_demo:
                generate_ai_video_replicate(scene_text, s_broll)
            else:
                download_pixabay_video(scene_text, s_broll)

            render_scene(s_broll, s_audio, avatar_path, s_scene_out, scene_text, is_demo=is_demo)
            scene_video_paths.append(s_scene_out)

        final_video = f"video_{job_id[:8]}.mp4"
        if len(scene_video_paths) == 1:
            if os.path.exists(final_video): os.remove(final_video)
            os.rename(scene_video_paths[0], final_video)
        else:
            concat_list = f"concat_{job_id}.txt"
            temp_files.append(concat_list)
            with open(concat_list, "w", encoding="utf-8") as f:
                for sv in scene_video_paths:
                    f.write(f"file '{os.path.abspath(sv).replace(chr(92), '/')}'\n")
            
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                final_video
            ]
            subprocess.run(cmd, check=True)
            for sv in scene_video_paths:
                if os.path.exists(sv): os.remove(sv)

        job_tracker[job_id] = {"status": "COMPLETED", "path": final_video}

        if user_id:
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO video_jobs (user_id, prompt, status, video_path) VALUES (:u_id, :prompt, 'COMPLETED', :path)"),
                    {"u_id": user_id, "prompt": prompt, "path": final_video}
                )

        print(f"--- [DONE] Video Ready: {final_video} ---\n")
    except Exception as e:
        print(f"[ERROR in Job {job_id}] {e}")
        job_tracker[job_id] = {"status": "FAILED", "path": None}
        if user_id:
            with engine.begin() as conn:
                conn.execute(text("UPDATE users SET credits = credits + 1 WHERE user_id = :u_id"), {"u_id": user_id})
    finally:
        for tf in temp_files:
            if os.path.exists(tf):
                try: os.remove(tf)
                except Exception: pass

@app.get("/", response_class=HTMLResponse)
def index_page():
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Global AI Video Studio & Multilingual Engine</title>
    <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; padding: 25px; line-height: 1.5; }}
        .container {{ max-width: 860px; margin: auto; }}
        .card {{ background: #131d31; padding: 25px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); margin-bottom: 25px; border: 1px solid #1e293b; }}
        h2, h3 {{ margin-top: 0; color: #38bdf8; }}
        label {{ font-size: 13px; font-weight: 600; color: #94a3b8; display: block; margin-top: 14px; }}
        input, textarea, select {{ width: 100%; padding: 11px; margin-top: 5px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }}
        button {{ width: 100%; margin-top: 15px; padding: 13px; background: #0284c7; color: white; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; }}
        button:hover {{ background: #0369a1; }}
        .avatar-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 8px; }}
        .avatar-card {{ background: #0f172a; border: 2px solid #334155; border-radius: 8px; padding: 8px; text-align: center; cursor: pointer; font-size: 12px; }}
        .avatar-card.selected {{ border-color: #38bdf8; background: #1e3a5f; }}
        .pricing-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; margin-top: 15px; }}
        .plan-box {{ background: #0f172a; border: 2px solid #334155; border-radius: 8px; padding: 15px; text-align: center; cursor: pointer; }}
        .plan-box.selected {{ border-color: #38bdf8; background: #1e3a5f; }}
        .badge {{ display: inline-block; padding: 3px 8px; font-size: 11px; background: #38bdf8; color: #000; border-radius: 4px; font-weight: bold; }}
        #status, #payStatus {{ margin-top: 15px; padding: 12px; border-radius: 6px; display: none; }}
        .success {{ background: #064e3b; color: #6ee7b7; border: 1px solid #059669; }}
        .error {{ background: #7f1d1d; color: #fca5a5; border: 1px solid #dc2626; }}
        video {{ width: 100%; border-radius: 8px; margin-top: 15px; }}
        .disclaimer-card {{ background: #090d16; border: 1px solid #374151; padding: 20px; border-radius: 8px; font-size: 12px; color: #9ca3af; line-height: 1.6; }}
        .disclaimer-card h4 {{ margin: 0 0 8px 0; color: #e5e7eb; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .disclaimer-list {{ margin: 8px 0 0 16px; padding: 0; }}
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h2>Global Text-to-Video Creator Studio</h2>
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span id="quotaInfo" style="font-size:13px; color:#cbd5e1;">Free Demos Remaining: <strong>3/3</strong></span>
            <span style="font-size:12px; color:#10b981; font-weight:600;">✓ Generative AI Pipeline Available</span>
        </div>

        <label>Visual Engine Mode</label>
        <select id="engineMode">
            <option value="stock" selected>Stock Visual Engine (Fast & Cost-Efficient)</option>
            <option value="generative">Cinematic Generative AI (Runway/MiniMax via Replicate)</option>
        </select>

        <label>Select Video Language / भाषा चुनें</label>
        <select id="languageSelect">
            <option value="en" selected>English (US/UK) - Global English</option>
            <option value="hi">Hindi (हिन्दी) - Native Hindi Voice</option>
            <option value="es">Spanish (Español)</option>
            <option value="fr">French (Français)</option>
            <option value="de">German (Deutsch)</option>
        </select>

        <label>Select In-Built Character Presenter</label>
        <div class="avatar-grid">
            <div id="av_devotional" class="avatar-card selected" onclick="selectAvatar('devotional')">
                <strong>Aarav</strong><br><span style="color:#94a3b8;">Spiritual & Cultural</span>
            </div>
            <div id="av_business" class="avatar-card" onclick="selectAvatar('business')">
                <strong>Sarah</strong><br><span style="color:#94a3b8;">Executive Anchor</span>
            </div>
            <div id="av_tech" class="avatar-card" onclick="selectAvatar('tech')">
                <strong>Alex</strong><br><span style="color:#94a3b8;">Tech Specialist</span>
            </div>
            <div id="av_creative" class="avatar-card" onclick="selectAvatar('creative')">
                <strong>Leo</strong><br><span style="color:#94a3b8;">Creator Host</span>
            </div>
        </div>

        <label>Script & Narration</label>
        <textarea id="prompt" rows="4">From the infinite cosmic waters came the first avatar of Lord Vishnu, Matsya. He arrived not only to protect the sacred Vedas, but to ignite the sacred journey of life itself. From the deep primal ocean, divine grace guided creation onto land, igniting the evolution of all living beings on Earth.</textarea>
        
        <label>Creator License Key (Optional - Leave blank for Free Demo)</label>
        <input type="text" id="licenseKey" value="E68D-AB69-E521-C669" />

        <button id="genBtn" onclick="submitVideo()">Generate Video</button>
        <div id="status"></div>
        <div id="videoBox" style="display:none; text-align:center;">
            <h4 style="color:#38bdf8; margin: 10px 0 0 0;">Generated Production Output</h4>
            <video id="player" controls autoplay playsinline style="width: 100%; max-height: 420px;"></video>
        </div>
    </div>

    <div class="card">
        <h3>Creator Commercial Plans & Global Top-Up</h3>
        <p style="color:#94a3b8; font-size:14px; margin-top:0;">Direct settlement via UPI, Global Cards, and Net Banking.</p>
        
        <label>Subscriber User ID</label>
        <input type="text" id="payUserId" value="b59fa494-2256-4625-8ece-d8884d89502f" />

        <div class="pricing-grid">
            <div id="planCreator" class="plan-box selected" onclick="selectPlan('creator')">
                <span class="badge" style="background:#10b981; color:#fff;">CREATOR LITE</span>
                <h4 style="margin:10px 0;">25 Long Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹999 <span style="font-size:12px; color:#94a3b8;">(~$12)</span></p>
            </div>
            <div id="planPro" class="plan-box" onclick="selectPlan('pro')">
                <span class="badge">PRO CREATOR</span>
                <h4 style="margin:10px 0;">100 Long Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹2,499 <span style="font-size:12px; color:#94a3b8;">(~$30)</span></p>
            </div>
            <div id="planStudio" class="plan-box" onclick="selectPlan('studio')">
                <span class="badge">STUDIO GLOBAL</span>
                <h4 style="margin:10px 0;">300 Long Videos</h4>
                <p style="color:#38bdf8; font-weight:bold; font-size:18px;">₹5,999 <span style="font-size:12px; color:#94a3b8;">(~$75)</span></p>
            </div>
        </div>

        <button style="background:#10b981;" onclick="payWithRazorpay()">Subscribe & Upgrade Credits</button>
        <div id="payStatus"></div>
    </div>

    <div class="disclaimer-card">
        <h4>International Legal Compliance, Synthetic Media & Safe Harbor Terms</h4>
        <p style="margin:0 0 8px 0;">
            This software operates as an automated digital rendering pipeline. By generating media on this service, you legally agree to:
        </p>
        <ul class="disclaimer-list">
            <li><strong>EU AI Act Article 50 Compliance:</strong> All generated media carries automated digital transparency watermarks disclosing synthetic AI voice and character composition.</li>
            <li><strong>Automated Ethical Filtering:</strong> Strictly blocks content containing harassment, defamation, intellectual property theft, deepfake deception, violence, illegal drugs, or hate speech.</li>
            <li><strong>Global Safe Harbor & User Liability:</strong> The creator retains sole civil and criminal accountability for script inputs, publication, and distribution under US DMCA, Indian IT Act, and EU Digital Services frameworks.</li>
        </ul>
    </div>
</div>

<script>
    let selectedPlan = 'creator';
    let selectedAvatar = 'devotional';
    let demoCount = 0;
    const rzpKeyId = "{RAZORPAY_KEY_ID}";

    function selectAvatar(av) {{
        selectedAvatar = av;
        ['devotional', 'business', 'tech', 'creative'].forEach(a => {{
            document.getElementById('av_' + a).classList.toggle('selected', a === av);
        }});
    }}

    function selectPlan(plan) {{
        selectedPlan = plan;
        document.getElementById('planCreator').classList.toggle('selected', plan === 'creator');
        document.getElementById('planPro').classList.toggle('selected', plan === 'pro');
        document.getElementById('planStudio').classList.toggle('selected', plan === 'studio');
    }}

    async function payWithRazorpay() {{
        const payStatus = document.getElementById('payStatus');
        payStatus.style.display = 'block';
        payStatus.className = '';
        payStatus.innerText = 'Initializing secure payment order...';

        const userId = document.getElementById('payUserId').value;
        const res = await fetch('/create-order', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ user_id: userId, plan: selectedPlan }})
        }});
        const orderData = await res.json();
        if (!res.ok) {{
            payStatus.className = 'error';
            payStatus.innerText = orderData.detail || 'Could not initiate checkout.';
            return;
        }}

        const options = {{
            "key": rzpKeyId,
            "amount": orderData.amount,
            "currency": "INR",
            "name": "Global Video Creator Studio",
            "description": selectedPlan.toUpperCase() + " Commercial License",
            "order_id": orderData.order_id,
            "handler": async function (response) {{
                payStatus.innerText = 'Verifying settlement with bank...';
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
                    payStatus.innerHTML = `<strong>Payment Confirmed!</strong><br>License Key: <code style="color:#38bdf8; font-weight:bold;">${{verifyData.license_key}}</code><br>Credits Added: +${{verifyData.credits}}<br><em>Key automatically filled into the studio generator!</em>`;
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
        const lang = document.getElementById('languageSelect').value;
        const mode = document.getElementById('engineMode').value;

        if (!prompt) return alert('Please enter a video prompt or script');

        status.style.display = 'block';
        status.className = '';
        status.innerText = 'Moderating script & processing video...';
        videoBox.style.display = 'none';
        btn.disabled = true;

        try {{
            const res = await fetch('/generate', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    prompt: prompt,
                    language: lang,
                    character: selectedAvatar,
                    engine_mode: mode,
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
            status.innerHTML = `<strong>Rendering ${{data.is_demo ? 'Demo Preview' : 'Production Video'}}...</strong><br>Job ID: ${{data.job_id}}`;
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
                    document.getElementById('status').innerHTML = '<strong>Production Video Ready!</strong>';
                    const player = document.getElementById('player');
                    player.src = `/download/${{data.video_path}}`;
                    player.muted = false;
                    player.volume = 1.0;
                    document.getElementById('videoBox').style.display = 'block';
                    document.getElementById('genBtn').disabled = false;
                }} else if (data.status === 'FAILED') {{
                    clearInterval(timer);
                    document.getElementById('status').className = 'error';
                    document.getElementById('status').innerText = 'Generation encountered an error during scene rendering.';
                    document.getElementById('genBtn').disabled = false;
                }}
            }} catch (err) {{
                console.error("Polling error:", err);
            }}
        }}, 2500);
    }}
</script>
</body>
</html>"""

@app.post("/create-order")
def create_order(req: CreateOrderRequest):
    plan_prices = {"creator": 99900, "pro": 249900, "studio": 599900}
    amount = plan_prices.get(req.plan.lower(), 99900)

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

    credits_map = {"creator": 25, "pro": 100, "studio": 300}
    credits = credits_map.get(req.plan.lower(), 25)
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
    character_choice = req.character or "devotional"
    language_choice = req.language or "en"
    engine_mode = req.engine_mode or "stock"

    job_id = secrets.token_hex(8)
    job_tracker[job_id] = {"status": "PROCESSING", "path": None}
    
    if req.license_key:
        with engine.begin() as conn:
            user = conn.execute(
                text("SELECT credits FROM users WHERE user_id = :u_id FOR UPDATE"),
                {"u_id": req.user_id}
            ).fetchone()
            if not user or user[0] <= 0:
                raise HTTPException(status_code=402, detail="Credits exhausted. Please top up your subscription.")
            
            conn.execute(
                text("UPDATE users SET credits = credits - 1 WHERE user_id = :u_id"),
                {"u_id": req.user_id}
            )
        
        background_tasks.add_task(process_job, req.user_id, validated_prompt, character_choice, language_choice, engine_mode, job_id, is_demo=False)
        return {"job_id": job_id, "is_demo": False}

    background_tasks.add_task(process_job, None, validated_prompt, character_choice, language_choice, "stock", job_id, is_demo=True)
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
