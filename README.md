# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer hiring assignment (Contract v2.0).

## Requirements
- Python 3.11+
- MongoDB 6.0+ (local server or MongoDB Atlas)

## Run on Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

For Atlas, replace `MONGO_URI` with your Atlas connection string. Never commit credentials or `.env` files. Environment variables take precedence over `.env` values.

Open `http://127.0.0.1:8000/docs` for interactive API docs. `GET /health` pings MongoDB and returns 503 when the database is unavailable. Indexes are created idempotently at startup.

## Included
- `app/main.py`: API endpoints and MongoDB aggregation pipelines
- `requirements.txt`: dependencies
- `REVIEW.md`: starter code review
- `DECISIONS.md`: design decisions

## Important
Run and verify the API against MongoDB 6.0+ before submitting. In particular, test parallel punch-in/punch-out requests, regularization history, aggregation results, and each explain plan on realistic data. The hidden dataset and evaluation tests are not available locally; do not claim the submission passes them without testing.
