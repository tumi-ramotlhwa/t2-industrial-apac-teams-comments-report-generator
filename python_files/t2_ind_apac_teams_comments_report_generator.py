import requests
import signal
import sys
import csv
from urllib.parse import urlparse, unquote
import re
import time
import logging
import os
from tenacity import retry, wait_exponential, stop_after_attempt
from datetime import date, timedelta, datetime, timezone
from dateutil import parser

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class IgnoreHTTP200Filter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "HTTP 200 for URL:" in msg:
            return False
        if "Reply fetch HTTP 200 for message" in msg:
            return False
        return True
0
logger = logging.getLogger()
for handler in logger.handlers:
    handler.addFilter(IgnoreHTTP200Filter())

TENANT_ID = '9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674'  
CLIENT_ID = 'd44a87ae-6731-45e9-84e3-ceb35005cc83'
REDIRECT_URI = os.getenv('REDIRECT_URI', 'http://localhost:8000')
SCOPES = 'ChannelMessage.Read.All offline_access'
TOKEN_URL = f'https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token'
PERFORMANCE_PERIOD = ''

failed_message_ids = []
failed_reply_message_ids = []
unique_parent_message_ids = set()

APAC_TEAMS_MESSAGES_FILE = f"apac_teams_messages_tracker_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"

def refresh_access_token(refresh_token):
    data = {
        'client_id': CLIENT_ID,
        'scope': SCOPES,
        'refresh_token': refresh_token,
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'refresh_token',
    }
    try:
        response = requests.post(TOKEN_URL, data=data)
        response.raise_for_status()
        tokens = response.json()
        logging.info("Access token refreshed successfully.\n\n")
        return tokens.get('access_token'), tokens.get('refresh_token')
    except requests.exceptions.RequestException as e:
        logging.error(f"Error refreshing access token: {e}\n\n")
        return None, None

html_tags_pattern = re.compile(r'<[^>]*>')
non_alphanumeric_pattern = re.compile(r'[^\w\s:/.-]')

def clean_content(content):
    if isinstance(content, str):
        html_entities = {
            '&nbsp;': ' ', '&amp;': '&', '&lt;': '<',
            '&gt;': '>', '&quot;': '"', '&#39;': "'"
        }
        for entity, replacement in html_entities.items():
            content = content.replace(entity, replacement)
        content = html_tags_pattern.sub('', content)
        content = non_alphanumeric_pattern.sub('', content)
    else:
        content = ''
    return content

def ordinal(n):
    """Return ordinal suffix for day numbers (e.g., 1st, 2nd, 3rd, 4th)"""
    return f"{n}{'tsnrhtdd'[(n//10%10!=1)*(n%10<4)*n%10::4]}"

def format_date(d):
    """Format date as '1st July 2025'"""
    return f"{ordinal(d.day)} {d.strftime('%B')} {d.year}"

def get_date_input(prompt):
    """Safely get a date from user input with validation"""
    while True:
        try:
            user_input = input(prompt).strip()
            # Parse using dateutil.parser (supports many formats)
            parsed_date = parser.isoparse(user_input).date()
            return parsed_date
        except Exception as e:
            print(f"Invalid date format. Please enter a valid date (e.g., '2025-01-15', '15 Jan 2025', 'January 15, 2025').")
            continue

def select_date_range():
    """
    Prompt user for custom start and end dates.
    Returns:
        [six_months_before, start, end] as timezone-aware datetime objects (UTC)
    """
    print("Enter a custom date range to retrieve Teams messages:")

    start_date = get_date_input("Enter start date (e.g., 2025-01-15, 15 Jan 2025): ")

    end_date = get_date_input("Enter end date (e.g., 2025-06-30, 30 Jun 2025): ")

    # Validate: end date must be >= start date
    if end_date < start_date:
        print("Error: End date cannot be before start date.")
        return None
    
    # # Calculate one year before start date
    # try:
    #     one_year_before = start_date.replace(year=start_date.year - 1)
    # except ValueError:
    #     # Handle leap year edge case: Feb 29 → Feb 28 in non-leap year
    #     one_year_before = start_date.replace(year=start_date.year - 1, day=28)

    # Calculate 6 months before start date (6 months for speed and efficiency)
    try:
        six_months_before = start_date.replace(month=start_date.month - 6)
    except ValueError:
        # Handle month rollover: Jan–Jun → previous year
        six_months_before = start_date.replace(year=start_date.year - 1, month=start_date.month + 6)

    # Convert to UTC-aware ISO format
    def to_utc_iso(dt):
        return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).isoformat()

    # Return parsed datetime objects (UTC)
    return [
        parser.isoparse(to_utc_iso(six_months_before)),
        parser.isoparse(to_utc_iso(start_date)),
        parser.isoparse(to_utc_iso(end_date))
    ]
 
# Retry the function with exponential backoff
@retry(wait=wait_exponential(multiplier=3, min=30, max=600), stop=stop_after_attempt(5))
def make_request_with_retry(url, headers, session, refresh_token=None):
    try:
        response = session.get(url, headers=headers)
        logging.warning(f"HTTP {response.status_code} for URL: {url}")

        # Handle token expiration (401)
        if response.status_code == 401 and refresh_token:
            logging.warning("Access token expired. Attempting to refresh...")
            new_token, new_refresh = refresh_access_token(refresh_token)
            if new_token:
                headers['Authorization'] = f'Bearer {new_token}'
                response = session.get(url, headers=headers)
                logging.warning(f"HTTP {response.status_code} after token refresh: {url}")
            else:
                logging.error("Failed to refresh token after 401.")
                response.raise_for_status()

        # Handle rate limiting (429)
        elif response.status_code == 429:
            retry_after = response.headers.get('Retry-After')

            if retry_after and retry_after.isdigit():
                # Use the provided Retry-After header if valid
                wait_time = int(retry_after)
            else:
                # If Retry-After is missing or invalid, apply exponential backoff
                wait_time = 15  # Start with initial backoff time

            logging.warning(f"Rate limit exceeded. Retrying after {wait_time} seconds...")
            time.sleep(wait_time)
            raise Exception("Retry due to rate limit")

        # Handle server errors (502 Bad Gateway)
        elif response.status_code == 502:
            logging.warning("Received 502 Bad Gateway. Retrying due to server error.")
            raise Exception("Retry due to 502 Bad Gateway")

        response.raise_for_status()
        return response

    except requests.exceptions.RequestException as e:
        logging.warning(f"Error making request: {e}")
        raise
        
def extract_channel_info(url):
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.split('/')
    channel_id_encoded = path_parts[3] if len(path_parts) > 3 else None
    channel_id = unquote(channel_id_encoded) if channel_id_encoded else None
    channel_name_encoded = path_parts[-1] if len(path_parts) > 1 else None
    channel_name_decoded = channel_name_encoded.replace('%20', '_').replace(' ', '_') if channel_name_encoded else None
    return channel_id, channel_name_decoded

def get_replies(message_id, access_token, team_id, channel_id, session, refresh_token):
    url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"
    headers = {'Authorization': f'Bearer {access_token}'}
    all_replies = []

    while url:
        try:
            response = make_request_with_retry(url, headers, session, refresh_token)
        except Exception as e:
            # logging.error(f"Reply fetch failed for message {message_id}: {e}")
            raise

        data = response.json()
        if 'value' not in data or not isinstance(data['value'], list):
            # logging.error(f"Invalid reply structure for message {message_id}")
            raise ValueError("Invalid reply structure")

        all_replies.extend(data['value'])
        url = data.get('@odata.nextLink')

    return all_replies

def save_to_csv(message_tracker, channel_name, t2_members, in_progress):
    try:
        with open(APAC_TEAMS_MESSAGES_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if in_progress:
                logging.info(f"Writing {channel_name} data to CSV\n")
                writer.writerow([''] + list(t2_members.keys()))
                writer.writerow([channel_name])
                for data in message_tracker:
                    writer.writerow([data[0], data[1], data[2], data[3], data[4], data[5], data[6], data[7], data[8], data[9], data[10], data[11], data[12]])
            else:
                logging.info(f"Writing total messages count to CSV\n")
                writer.writerow([f"TOTAL MESSAGES FOR {PERFORMANCE_PERIOD.upper()}"] + [member["total_messages"] for member in t2_members.values()])
           
            writer.writerow([]) 

    except Exception as e:
        print(e)
        
def get_team_messages(access_token, refresh_token, team_id, channel_id, channel_name, team_name, session, dates, t2_members):
    url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"
    headers = {'Authorization': f'Bearer {access_token}'}


    lookback_date = dates[0]
    start_date = dates[1]
    end_date = dates[2]

    old_msg = 0
    t2_messages_tracker = []
    
    while url:
        try:
            response = make_request_with_retry(url, headers, session, refresh_token)
            messages = response.json()
            messages_data = messages["value"]

            # Only keep messages before the end date
            messages_filtered = list(filter(lambda m: datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00")) <= end_date, messages_data))

            # Sort messages in reverse chronological order (newest to oldest)
            messages_sorted = sorted(messages_filtered, key=lambda m: datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00")), reverse=True)

            for msg in messages_sorted:

                msg_date = parser.isoparse(msg['createdDateTime'].replace("Z", "+00:00"))
                logging.info(f"Checking next message at {channel_name}, date {msg_date}")
                
                # 3 is an arbituary value, if more than 3 threads go beyond the lookback date, it is safe to say the search has gone far enough
                if msg_date < lookback_date: 
                    old_msg += 1
                    if old_msg > 3:
                        t2_messages_tracker.append(['TOTAL:'] + [member['channel_messages'] for member in t2_members.values()])
                        for member in t2_members: t2_members[member]["channel_messages"] = 0
                        return t2_messages_tracker

                # Count data if a T2 member made a teams channel thread inside the selected date range
                if (msg['deletedDateTime'] is None) and \
                    (msg['messageType'] == 'message') and \
                    (msg_date >= start_date) and \
                    (msg_date <= end_date) and \
                    (msg["from"]["user"]["displayName"] in t2_members):
                    t2_members[msg["from"]["user"]["displayName"]]["thread_messages"] += 1
                    t2_members[msg["from"]["user"]["displayName"]]["channel_messages"] += 1
                    t2_members[msg["from"]["user"]["displayName"]]["total_messages"] += 1
                    #print(msg, "\n")

                replies = get_replies(msg['id'], access_token, team_id, channel_id, session, refresh_token)
                for reply in replies:
                    reply_date = parser.isoparse(reply['createdDateTime'].replace("Z", "+00:00"))
                    #print(reply, "\n")

                    # Count data if a T2 member made a teams reply to the channel thread inside the selected date range
                    if (reply['deletedDateTime'] is None) and \
                        (reply['messageType'] == 'message') and \
                        (reply_date >= start_date) and \
                        (reply_date <= end_date) and \
                        (reply["from"]["user"]["displayName"] in t2_members):  
                        t2_members[reply["from"]["user"]["displayName"]]["thread_messages"] += 1
                        t2_members[reply["from"]["user"]["displayName"]]["channel_messages"] += 1
                        t2_members[reply["from"]["user"]["displayName"]]["total_messages"] += 1
                
                # Reset all team members' thread message count to 0 when thread search is complete
                if sum(member["thread_messages"] for member in t2_members.values()) > 0:
                    t2_messages_tracker.append([msg['webUrl']] + [member['thread_messages'] for member in t2_members.values()])
                    for member in t2_members: t2_members[member]["thread_messages"] = 0

            # Continue to the next page of messages if available
            url = messages.get('@odata.nextLink')
            logging.info(f"Fetched next page of messages at {channel_name}")

        except Exception as e:
            logging.error(f"Error fetching messages for team {team_name}, channel {channel_name}: {e}")
            break
    
    logging.info(f"Fetching messages for {channel_name} complete")

    # Reset all team member's channel message count to 0 when the channel search is complete
    t2_messages_tracker.append(['TOTAL:'] + [member['channel_messages'] for member in t2_members.values()])
    for member in t2_members: t2_members[member]["channel_messages"] = 0

    return t2_messages_tracker
    
# Graceful shutdown to handle script termination
def handle_shutdown_signal(signal, frame):
    logging.info("Gracefully shutting down.")
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, handle_shutdown_signal)  # Handle interrupt signal (Ctrl+C)

def main():
    refresh_token = input("Enter your refresh token: ")
    new_access_token, new_refresh_token = refresh_access_token(refresh_token)

    if not new_access_token:
        logging.error("Error obtaining new access token.")
        return

    session = requests.Session()

    dates = select_date_range()

    team_name = "APAC Service Operations"
    team_id = "1c77ec90-9f5a-4cf6-8337-e75399c26770"
    channel_urls = [
        "https://teams.microsoft.com/l/channel/19%3Aeff5783feab041eb8ea7402808e9e318%40thread.skype/Bouldercombe%20(Big%20Bessie)%20SM-15009?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A74535d7880ad47279359f3d816162b9e%40thread.skype/Brendale%20SM-23258?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Aca5f621b6291407ba1a5bba83d9191bb%40thread.skype/Brendale%20SM-23716?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Af1333832ae254bbcb2fc1a45a2af949d%40thread.skype/Bulgana%20SM-9539?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A774404416c53445eb9a3e9be39b0da63%40thread.skype/Chinchilla%20SM-17561?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A6d37e30c1c984977aeec17b2dd3cd1f5%40thread.skype/Collie%202%20BESS%20STST-SM-24656?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Ab06bbd86796f4bbfb66b240eb34c3ea8%40thread.skype/Collie%20BESS%20SM-13700?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A02fbd37c10fe4165a2ac308e4c42be43%40thread.skype/Equis%20MREH%20STST-SM-22168?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Ab35925bed41644e8bb65df6b8d2f3ac2%40thread.skype/Fiji?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A455f58f132c84bcdbea349b92a9022db%40thread.skype/Gannawarra%20SM-9518?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A89e2aceae7e94ecebd12a76868d1e499%40thread.skype/Glenbrook%20NZ%20STST-SM-24017?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A6df0e2e7f48341a2ae5e39861f86e8a5%40thread.skype/Greenbank%20SM-17633?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Ae0e6e9e622ce4fe59a7ffdfad63b9a7b%40thread.skype/Hornsdale%20SM-9511?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A038fe5580f7847a2b01d48814cfd6d9c%40thread.skype/Iberdrola%20Smithfield%20SM-23258?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Aa8d088fd495c43aca7a5a44fe3af1c40%40thread.skype/Japan?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A22b35c9acbe44470ab4c1f90d04f8fdf%40thread.skype/Koorangie%20STST-SM-21458?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Ac45aba04c57f4218aaac211dc997cf1b%40thread.skype/Lake%20Bonney%20SM-9529?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3ArIfQG73s6UhKug-0YwOx-BqRSdzkcIn9wn0mVY8rM3c1%40thread.skype/Limondale%20STST-SM-23269?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674&ngc=true&allowXTenantAccess=true",
        "https://teams.microsoft.com/l/channel/19%3Ad786b48234a54440b2caa615f9b29e1c%40thread.skype/Megapack%202%20General?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A0592c8f082974ade9bec5dc9a87b0b0e%40thread.skype/Megapack%20General?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A6df347b6ed8a4cb38d5e0677ede24b03%40thread.skype/MicroGrids?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A5a8761e78d214c1b83f2479a574bd3c5%40thread.skype/New%20Zealand?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A675a763ae1c541f3a2617f61f38861ba%40thread.skype/Niue%20SM-16114?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A08f973d19b954641b60079bd25ec7755%40thread.skype/Philippines?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A0f148598d7144cada9994321ea83da8e%40thread.skype/Powerpack%20General?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Adebbeb29089242cc848c148b0c63cbe5%40thread.skype/Rarotonga?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A94188b51caf84146a1acd965d9ea4b9e%40thread.skype/Riverina%20Darlington%20Point%20TBX-7410200?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A82493a869a134dea8755dbb4ea3953e9%40thread.skype/Samoa?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Aa15d8f15e52e4cdab5c70e78d7620dda%40thread.skype/Singapore?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Ada2ec6f0fdda4e3db2c3c2d5c644f224%40thread.skype/Solomon%20Islands?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A9adbd3241d8448b9958e4c32df9cc76a%40thread.skype/South%20Korea?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A1300469af66845c987584594a5f32707%40thread.skype/Tahiti?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A22355ff775374a3d8a16ebf0eeb39f28%40thread.skype/Taiwan?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A3bd4a1f85b8b4355a28fbc4f97e9c1bc%40thread.skype/Tarong%20BESS%20SM-17559?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A1340c73e5e6e400a82d698d727e10fdb%40thread.skype/Tarong%20SM-17559?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Aa94fc8aec31d4a24b77a52b74ee5c0e6%40thread.skype/Tonga?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Adfcca2ae712b48d6a6096f42eaf6089b%40thread.skype/TW-%20ChingJia%20-%20CH%20Chuansing%20(11%20TSC)?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A30415c8536bd4617a0304a855c7db07b%40thread.skype/TW-%20CY%20Shuishang%20-%20SM-23980?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A121e5ffb83e34ab3bd88fffd693c798c%40thread.skype/TW-%20HL%20Meilun%20(4h)%20SM-19142?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674"
        "https://teams.microsoft.com/l/channel/19%3A3fd0724787ff4483b10311faa627ed3b%40thread.skype/TW-%20YL%20Douliu%20(4h)%20SM-20584?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3Aafc509a670ff4646b85654eaaf0f8296%40thread.skype/Victorian%20Big%20Battery%20SM-9541?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A78d9ecb98da2409fb4abba5896d87026%40thread.skype/Wallgrove%20(Sydney%20West)%20SM-9538?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A065650a05fbe4502aabe44229349a0f3%40thread.skype/Western%20Downs%202%20STST-SM-23410?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674",
        "https://teams.microsoft.com/l/channel/19%3A32ffd48efda44e0fb13f0a6f9f711712%40thread.skype/Western%20Downs%20SM-11027?groupId=1c77ec90-9f5a-4cf6-8337-e75399c26770&tenantId=9026c5f4-86d0-4b9f-bd39-b7d4d0fb4674"
    ]
    t2_members = {
        "Jarrod Barnes": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Ricky Chan": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Bowen Huo": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
         "Keyur Shah": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Derek Liu": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Richard Park": {   
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Tomoaki Yamasaki (山崎 智明)": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Wilson Nien (粘 朝崴)": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Tumi Ramotlhwa": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Zachariah Couch": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Jankin Wang": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
        "Sheng Wen Qiu": {
            "thread_messages": 0,
            "channel_messages": 0,
            "total_messages": 0,
        },
    }

    for channel_url in channel_urls:
        channel_id, channel_name = extract_channel_info(channel_url)
        logging.info(f"Extracted Channel ID: {channel_id}")
        logging.info(f"Extracted Channel Name: {channel_name}")

        if channel_id:
            logging.info(f"Fetching messages for: {channel_name}")
            t2_messages_tracker = get_team_messages(new_access_token, new_refresh_token, team_id, channel_id, channel_name, team_name, session, dates, t2_members)
            save_to_csv(t2_messages_tracker, channel_name, t2_members, in_progress=True)
        else:
            logging.error(f"Invalid channel URL: {channel_url}")
    
    save_to_csv(None, channel_name, t2_members, in_progress=False)
    logging.info(f"Finished.\n")

if __name__ == "__main__":
    main()