# Project Guidelines

## Code Style
- Use Python with FastAPI for API development.
- Follow Pydantic for data models and validation.
- Normalize emails to lowercase and strip whitespace.
- Use environment variables for all secrets and configuration.
- Handle exceptions with HTTPException for API errors.

## Architecture
- FastAPI backend handling user subscriptions via Stripe webhooks.
- Supabase as the database for user data storage.
- CORS enabled for cross-origin requests.
- Webhook endpoint for Stripe events to update user access.

## Build and Test
- Install dependencies: `pip install -r requirements.txt`
- Run the server: `uvicorn main:app --reload` or `python main.py`
- No automated tests present; manual testing via API endpoints.

## Conventions
- User access controlled by `access_active` flag in Supabase.
- Subscription statuses: active, inactive, canceled, payment_failed.
- Webhook handles checkout completion, subscription updates, deletions, and payment failures.