from flask import Flask, request, jsonify, render_template
import google.generativeai as genai
from supabase import create_client, Client
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import logging
import json
import os
import pytz
import re

# Load configuration
try:
    from config import GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY, CREDENTIALS_FILE, TOKEN_FILE, SCOPES
except ImportError:
    GEMINI_API_KEY = None
    SUPABASE_URL = None
    SUPABASE_KEY = None
    CREDENTIALS_FILE = "credentials.json"
    TOKEN_FILE = "token.json"
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔹 Configure Gemini API
api_key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
if not api_key:
    logger.error("Gemini API key not found")
    llm = None
else:
    try:
        genai.configure(api_key=api_key)
        llm = genai.GenerativeModel('gemini-1.5-flash-001-tuning')
    except Exception as e:
        logger.error(f"Gemini API config error: {e}")
        llm = None

# 🔹 Configure Supabase
supabase_url = os.getenv("SUPABASE_URL", SUPABASE_URL)
supabase_key = os.getenv("SUPABASE_KEY", SUPABASE_KEY)
if not supabase_url or not supabase_key:
    logger.error("Supabase credentials not found")
    supabase = None
else:
    try:
        supabase: Client = create_client(supabase_url, supabase_key)
        supabase.table("meetings").select("*").limit(1).execute()
    except Exception as e:
        logger.error(f"Supabase connection failed: {e}")
        supabase = None

# 🔹 Google Calendar API Setup
def get_calendar_service():
    """Authenticate and return Google Calendar service."""
    credentials_file = os.getenv("CREDENTIALS_FILE", CREDENTIALS_FILE)
    token_file = os.getenv("TOKEN_FILE", TOKEN_FILE)
    if not os.path.exists(credentials_file):
        logger.error(f"Missing {credentials_file}")
        return None
    try:
        creds = None
        if os.path.exists(token_file):
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Calendar auth error: {e}")
        return None

# 🔹 In-memory state
state = {
    'chat_history': [],
    'meeting_details': {},
    'calendar_service': None
}

# 🔹 Helper Functions
def get_gemini_response(prompt):
    """Generate response using Gemini."""
    if not llm:
        return "Gemini API not initialized."
    try:
        response = llm.generate_content(prompt)
        if hasattr(response, 'text') and response.text:
            return response.text.strip()
        return "Failed to generate response."
    except Exception as e:
        logger.error(f"Gemini response failed: {str(e)}")
        return f"Oops, something went wrong: {str(e)}. Could you try again?"

def schedule_meeting(details):
    """Schedule a meeting in Google Calendar and store in Supabase."""
    try:
        service = state['calendar_service'] or get_calendar_service()
        if not service:
            return None, "Looks like your Google Calendar credentials are missing or invalid. Please ensure credentials.json is in the project directory and re-authenticate."
        state['calendar_service'] = service

        title = details.get('title', 'Meeting')
        if not isinstance(title, str) or not title.strip():
            logger.warning(f"Invalid title found: {title}. Defaulting to 'Meeting'.")
            title = 'Meeting'

        description = details.get('description', '')
        agenda = details.get('agenda', '')
        if agenda:
            description = f"{description}\n\nAgenda: {agenda}" if description else f"Agenda: {agenda}"

        logger.info(f"Scheduling meeting with details: {details}")

        event = {
            'summary': title,
            'description': description,
            'start': {
                'dateTime': details['start_time'].isoformat(),
                'timeZone': details.get('timezone', 'Asia/Kolkata'),
            },
            'end': {
                'dateTime': (details['start_time'] + timedelta(minutes=60)).isoformat(),
                'timeZone': details.get('timezone', 'Asia/Kolkata'),
            },
            'attendees': [{'email': email} for email in details.get('attendees', [])],
            'visibility': 'default',
            'status': 'confirmed',
            'reminders': {
                'useDefault': True
            }
        }
        event = service.events().insert(
            calendarId='primary',
            body=event,
            sendNotifications=True
        ).execute()
        logger.info(f"Event created: {event['id']}, Summary: {event['summary']}, Start: {event['start']['dateTime']}")

        if supabase:
            try:
                supabase.table("meetings").insert({
                    'event_id': event['id'],
                    'title': title,
                    'start_time': details['start_time'].isoformat(),
                    'description': description,
                    'attendees': details.get('attendees', []),
                    'agenda': agenda
                }).execute()
            except Exception as e:
                logger.error(f"Supabase insert failed: {str(e)}")
                return event['id'], "Meeting scheduled, but failed to store in Supabase."

        return event['id'], None
    except Exception as e:
        logger.error(f"Meeting scheduling failed: {str(e)}")
        return None, f"Oops, I couldn’t schedule the meeting: {str(e)}. Please check if credentials.json is valid, re-authenticate if needed, and ensure your Google Calendar is accessible."

# 🔹 Routes
@app.route('/')
def index():
    """Serve the frontend."""
    return render_template('index.html')

@app.route('/transcribe', methods=['POST'])
def transcribe():
    """Process speech or text input and return meeting details or schedule."""
    user_input = request.json.get('input')
    if not user_input:
        return jsonify({
            'error': 'No input provided.',
            'chat_history': state['chat_history']
        }), 400

    state['chat_history'].append({'role': 'user', 'message': user_input})
    
    prior_details = state.get('meeting_details', {})
    prior_details_str = json.dumps(prior_details, default=str) if prior_details else "None"

    prompt = f"""
    You're a warm, friendly meeting scheduler assistant, like a helpful colleague. The user said: "{user_input}"

    Current date is May 18, 2025. Previous meeting details (if any): {prior_details_str}

    Validate and correct the meeting details (title, date, time, timezone, description, agenda, attendees) from the user's message. Follow these rules:
    - If the input modifies an existing meeting (e.g., "title as [new title]", "add attendee"), update only the specified fields and retain other prior details unless explicitly changed.
    - Extract the title if specified; default to "Meeting" if not specified or unclear. Use prior title if input only updates other fields.
    - Parse date (e.g., "tomorrow" as 2025-05-19, "22 May" as 2025-05-22); ensure it’s on or after May 18, 2025; use prior date if not specified; default to May 18, 2025, only if no prior date and input is unclear.
    - Parse time in 12-hour (e.g., "9:00 a.m.") or 24-hour format; use prior time if not specified; default to 09:00 if unclear.
    - Default timezone to Asia/Kolkata if not specified or invalid; retain prior timezone if available.
    - Extract description if provided; use prior description if not specified; set to empty string if none.
    - If the user requests "points" or an agenda (e.g., "give some points") or if the title implies a topic (e.g., "machine learning"), generate a default agenda based on the title (e.g., for "machine learning": "1. Overview of machine learning\n2. Use cases\n3. Challenges\n4. Latest advancements\n5. Future directions"); otherwise, use prior agenda or set to empty string.
    - Parse attendees from natural language (e.g., "Maithili geek@gmail.com" as "maithiligeek@gmail.com"). For names without emails (e.g., "Ramesh"), assign dummy emails (e.g., "ramesh@example.com") and note in the message that emails were assumed. Retain prior attendees unless explicitly changed or removed.
    - Return corrected details in JSON format *only*:
      ```json
      {{
        "title": "<corrected_title>",
        "date": "<YYYY-MM-DD>",
        "time": "<HH:MM>",
        "timezone": "<valid_timezone>",
        "description": "<corrected_description>",
        "agenda": "<corrected_agenda>",
        "attendees": ["<email1>", "<email2>", ...]
      }}
      ```
    If the user says something like "confirm", "it is confirmed", "schedule it", "set the meeting", or variations (e.g., "confirm confirm", "please set the meeting"), return "SCHEDULE" to schedule immediately.
    If the input is unclear or lacks sufficient details, return "CLARIFY: Hmm, I couldn’t catch all the details. Could you clarify the title, date, or attendees?"

    Respond with *only* the JSON string, "SCHEDULE", or "CLARIFY:<message>" to avoid parsing issues. Do not include conversational text outside the JSON or CLARIFY message.
    """
    response = get_gemini_response(prompt)
    logger.info(f"Gemini response: {response}")

    cleaned_response = response.replace('```json', '').replace('```', '').strip()
    conversational_message = None
    json_str = None

    json_match = re.search(r'\{[\s\S]*\}', cleaned_response)
    if json_match:
        json_str = json_match.group(0)
        conversational_message = cleaned_response.replace(json_str, '').strip()
        logger.info(f"Extracted JSON: {json_str}")
        logger.info(f"Conversational message: {conversational_message or 'None'}")
    else:
        json_str = cleaned_response

    try:
        if cleaned_response == "SCHEDULE":
            if state['meeting_details']:
                event_id, error = schedule_meeting(state['meeting_details'])
                if event_id:
                    message = f"All done! Your meeting’s scheduled with Event ID: {event_id}. Check your Google Calendar and email for the details!"
                    state['meeting_details'] = {}
                else:
                    message = error
                state['chat_history'].append({'role': 'Assistant', 'message': message})
                logger.info(f"Constructed message: {message}")
                return jsonify({
                    'message': message,
                    'event_id': event_id,
                    'chat_history': state['chat_history']
                })
        elif cleaned_response.startswith("CLARIFY"):
            message = cleaned_response
            state['chat_history'].append({'role': 'Assistant', 'message': message})
            logger.info(f"Constructed message: {message}")
            return jsonify({
                'message': message,
                'event_id': None,
                'chat_history': state['chat_history']
            })
        else:
            corrected_details = json.loads(json_str)
            start_time = datetime.strptime(
                f"{corrected_details['date']} {corrected_details['time']}",
                '%Y-%m-%d %H:%M'
            ).replace(tzinfo=pytz.timezone(corrected_details['timezone']))
            state['meeting_details'] = {
                'title': corrected_details['title'] or state['meeting_details'].get('title', 'Meeting'),
                'description': corrected_details['description'] or state['meeting_details'].get('description', ''),
                'agenda': corrected_details['agenda'] or state['meeting_details'].get('agenda', ''),
                'start_time': start_time,
                'attendees': corrected_details['attendees'] or state['meeting_details'].get('attendees', []),
                'timezone': corrected_details['timezone'] or state['meeting_details'].get('timezone', 'Asia/Kolkata')
            }
            logger.info(f"Updated meeting details: {state['meeting_details']}")
            attendees_str = ', '.join(state['meeting_details']['attendees']) if state['meeting_details']['attendees'] else 'no attendees'
            description_str = state['meeting_details']['description'] or 'none'
            agenda_str = state['meeting_details']['agenda'] or 'none'
            message = f"Sir, I’ve got your {state['meeting_details']['title']} set for {start_time.strftime('%Y-%m-%d %H:%M')} {state['meeting_details']['timezone']} with {attendees_str}. Description: {description_str}. Agenda: {agenda_str}. Just say 'Confirm the meeting' to lock it in, or tweak it in the form!"
            if conversational_message and conversational_message.strip():
                message = conversational_message
            state['chat_history'].append({'role': 'Assistant', 'message': message})
            logger.info(f"Constructed message: {message}")
            return jsonify({
                'message': message,
                'event_id': None,
                'chat_history': state['chat_history']
            })
    except json.JSONDecodeError:
        logger.error(f"JSON decode error for response: {response}")
        message = "Oops, I had trouble understanding your meeting details. Could you clarify the title, date, or attendees?"
        state['chat_history'].append({'role': 'Assistant', 'message': message})
        logger.info(f"Constructed message: {message}")
        return jsonify({
            'message': message,
            'event_id': None,
            'chat_history': state['chat_history']
        })
    except Exception as e:
        logger.error(f"Error processing Gemini response: {str(e)}, Response: {response}")
        message = f"Hmm, something went wrong while processing your request: {str(e)}. Could you try again with the details?"
        state['chat_history'].append({'role': 'Assistant', 'message': message})
        logger.info(f"Constructed message: {message}")
        return jsonify({
            'message': message,
            'event_id': None,
            'chat_history': state['chat_history']
        })

@app.route('/schedule', methods=['POST'])
def schedule():
    """Schedule the current meeting details."""
    if not state['meeting_details']:
        message = "Hmm, I don’t have any meeting details to schedule yet. Try sharing some details first!"
        state['chat_history'].append({'role': 'Assistant', 'message': message})
        logger.info(f"Constructed message: {message}")
        return jsonify({
            'message': message,
            'event_id': None,
            'chat_history': state['chat_history']
        })
    
    event_id, error = schedule_meeting(state['meeting_details'])
    if event_id:
        message = f"All done! Your meeting’s scheduled with Event ID: {event_id}. Check your Google Calendar and email for the details!"
        state['meeting_details'] = {}
    else:
        message = error
    state['chat_history'].append({'role': 'Assistant', 'message': message})
    logger.info(f"Constructed message: {message}")
    return jsonify({
        'message': message,
        'event_id': event_id,
        'chat_history': state['chat_history']
    })

if __name__ == '__main__':
    app.run()