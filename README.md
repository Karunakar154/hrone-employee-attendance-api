# Employee Attendance & Analytics API

FastAPI and MongoDB API for employee records, attendance punch-in/punch-out, regularization, and attendance analytics. The implementation follows the rules in `openapi.yaml` and stores data in the `employees` and `attendance_logs` collections.

## Run locally

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Set `MONGO_URI` and `MONGO_DB` in your environment (or in a local `.env`, which must not be committed).
3. Ensure MongoDB 6.0+ is running and seed the sample data with `python sample_seed.py` if needed.
4. Start the API from the repository root: `uvicorn app.main:app --port 8000`
5. Open `http://127.0.0.1:8000/docs` for interactive API documentation. `GET /health` checks MongoDB readiness.

See `REVIEW.md` for starter defects and fixes, and `DECISIONS.md` for design choices.