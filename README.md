# Text-to-Video SaaS Platform

A zero-token, low-latency Text-to-Video generation engine powered by FastAPI, local FFmpeg rendering, and Windows SAPI speech synthesis, featuring automated content moderation and Razorpay settlement.

## Features
- **Local Audio Narration:** Windows SAPI TTS generation without cloud token costs.
- **Dynamic B-Roll Selection:** Automated stock footage matching via Pixabay API with intelligent keyword fallbacks.
- **Fast Multithreaded Rendering:** FFmpeg pipeline optimized with `-preset ultrafast` and multi-core acceleration for sub-10s turnaround.
- **Monetization & Licensing:** Integrated Razorpay checkout supporting UPI/Cards with instant cryptographic license key generation and PostgreSQL credit tracking.
- **Safety & Compliance:** Automated regex-based content moderation blocking malicious, abusive, or unlawful prompt generation.

## Tech Stack
- **Backend:** Python, FastAPI, Uvicorn, SQLAlchemy, psycopg2
- **Database:** PostgreSQL
- **Video & Audio:** FFmpeg, Windows SAPI, Pixabay API
- **Payments:** Razorpay API (INR)

## Setup & Running
1. Clone the repository:
   ```bash
   git clone [https://github.com/rakeshsingh07-png/text-to-video-saas.git](https://github.com/rakeshsingh07-png/text-to-video-saas.git)
