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
        [one_year_before, start, end] as timezone-aware datetime objects (UTC)
    """
    print("Enter a custom date range to retrieve Teams messages:")

    # Get start date
    start_date = get_date_input("Enter start date (e.g., 2025-01-15, 15 Jan 2025): ")

    # Get end date
    end_date = get_date_input("Enter end date (e.g., 2025-06-30, 30 Jun 2025): ")

    # Validate: end date must be >= start date
    if end_date < start_date:
        print("Error: End date cannot be before start date.")
        return None

    # Calculate one year before start date
    try:
        one_year_before = start_date.replace(year=start_date.year - 1)
    except ValueError:
        # Handle leap year edge case: Feb 29 → Feb 28 in non-leap year
        one_year_before = start_date.replace(year=start_date.year - 1, day=28)

    # Convert to UTC-aware ISO format
    def to_utc_iso(dt):
        return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).isoformat()

    # Return parsed datetime objects (UTC)
    return [
        parser.isoparse(to_utc_iso(one_year_before)),
        parser.isoparse(to_utc_iso(start_date)),
        parser.isoparse(to_utc_iso(end_date))
    ]

select_date_range()