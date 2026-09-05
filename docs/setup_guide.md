
🛠️ Local Setup & Deployment Guide
Prerequisites
Docker & Docker Compose
Node.js v18+ (for local frontend development)
Python 3.10+ (for local backend development)
Containerized Deployment (Recommended)

The entire stack is configured via docker-compose.yml. To launch the application cleanly:

Wipe previous state & build fresh:
docker compose down -v --remove-orphans
docker compose build --no-cache
Start the stack:
docker compose up -d
Verify Services:
Frontend: http://localhost:3000
Backend API Docs: http://localhost:8000/docs
PostgreSQL: localhost:5432
Generating Test Data

To test the AI exception engine, generate a fresh 60-record dataset by running the included python script in the root directory:

python generate_test_csv.py

This will generate test_batch_60_comprehensive.csv containing perfect matches, monetary variances, and status clashes.
