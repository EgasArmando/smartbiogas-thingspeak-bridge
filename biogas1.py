import os
import math
import requests
from datetime import datetime, timedelta, timezone


# ============================================================
# 1. ENVIRONMENT VARIABLES
# ============================================================

SMARTBIOGAS_API_KEY = os.getenv("SMARTBIOGAS_API_KEY")
THINGSPEAK_WRITE_KEY = os.getenv("THINGSPEAK_WRITE_KEY")
THINGSPEAK_CHANNEL_ID = os.getenv("THINGSPEAK_CHANNEL_ID", "")
THINGSPEAK_READ_KEY = os.getenv("THINGSPEAK_READ_KEY", "")

METER_ID = os.getenv("METER_ID", "mg6gx43")


# ============================================================
# 2. API SETTINGS
# ============================================================

SMARTBIOGAS_URL = (
    "https://api.smartbiogas.io/api/v1/gas-meter-reports"
)

THINGSPEAK_UPDATE_URL = "https://api.thingspeak.com/update"

# Search for records from the previous 24 hours
LOOKBACK_HOURS = 24

# Reject records older than this threshold.
# Because the workflow runs every 20 minutes, 60 minutes provides
# tolerance for API and GitHub Actions delays.
MAX_RECORD_AGE_MINUTES = 60

# Require at least this many valid sensor values.
MINIMUM_VALID_FIELDS = 1


# ============================================================
# 3. GENERAL FUNCTIONS
# ============================================================

def require_env(name, value):
    """Ensure that a required environment variable is available."""

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )


def utc_now():
    """Return the current timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def parse_timestamp(value):
    """
    Convert an ISO 8601 timestamp to a timezone-aware UTC datetime.
    """

    if not value:
        return None

    try:
        timestamp = str(value).strip()

        # Convert the common UTC Z suffix to an ISO-compatible offset
        timestamp = timestamp.replace("Z", "+00:00")

        parsed = datetime.fromisoformat(timestamp)

        # Assume UTC when the API returns a timestamp without a timezone
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def normalize_timestamp(value):
    """
    Convert a timestamp to a consistent UTC ISO 8601 representation.
    """

    parsed = parse_timestamp(value)

    if parsed is None:
        return None

    return parsed.isoformat(timespec="seconds")


def is_valid_number(value):
    """
    Return True when a value is a real finite number.

    Zero is considered valid because a meter can legitimately report
    zero flow when gas is not currently moving.
    """

    if value is None:
        return False

    if isinstance(value, bool):
        return False

    try:
        number = float(value)
        return math.isfinite(number)

    except (TypeError, ValueError):
        return False


# ============================================================
# 4. RECORD VALIDATION
# ============================================================

def validate_reading(reading):
    """
    Validate a Smart Biogas reading before uploading it.

    Identical measurement values are allowed. Duplicate detection is
    based only on the source timestamp.
    """

    if not isinstance(reading, dict):
        return False, "The API record is not a dictionary."

    timestamp = normalize_timestamp(reading.get("timestamp"))

    if timestamp is None:
        return False, "The record has no valid timestamp."

    record_time = parse_timestamp(timestamp)

    if record_time is None:
        return False, "The record timestamp could not be parsed."

    current_time = utc_now()
    record_age = current_time - record_time

    # Reject timestamps significantly in the future
    if record_age < timedelta(minutes=-5):
        return False, (
            "The record timestamp is in the future: "
            f"{timestamp}"
        )

    # Avoid repeatedly storing stale or null data when the meter is off
    if record_age > timedelta(minutes=MAX_RECORD_AGE_MINUTES):
        age_minutes = record_age.total_seconds() / 60

        return False, (
            f"The latest meter record is stale "
            f"({age_minutes:.1f} minutes old). "
            "The meter may be offline."
        )

    measurement_fields = {
        "flowLph": reading.get("flowLph"),
        "volumeL": reading.get("volumeL"),
        "staticPressurePa": reading.get("staticPressurePa"),
        "batteryV": reading.get("batteryV"),
        "solarV": reading.get("solarV"),
        "rssiDb": reading.get("rssiDb"),
    }

    valid_fields = {
        name: value
        for name, value in measurement_fields.items()
        if is_valid_number(value)
    }

    if len(valid_fields) < MINIMUM_VALID_FIELDS:
        return False, (
            "The record contains no valid sensor measurements. "
            "Upload skipped."
        )

    return True, "Record is valid."


# ============================================================
# 5. SMART BIOGAS DATA RETRIEVAL
# ============================================================

def get_latest_biogas_reading():
    """
    Retrieve the latest available Smart Biogas record.
    """

    require_env(
        "SMARTBIOGAS_API_KEY",
        SMARTBIOGAS_API_KEY,
    )

    end_at = utc_now()
    start_at = end_at - timedelta(hours=LOOKBACK_HOURS)

    params = {
        "gasMeterId": METER_ID,
        "startAt": start_at.isoformat(timespec="seconds"),
        "endAt": end_at.isoformat(timespec="seconds"),
    }

    headers = {
        "api-key": SMARTBIOGAS_API_KEY.strip(),
        "Accept": "application/json",
    }

    print("=" * 60)
    print(f"Connecting to Smart Biogas meter: {METER_ID}")
    print(
        "GitHub execution time UTC: "
        f"{end_at.isoformat(timespec='seconds')}"
    )

    try:
        response = requests.get(
            SMARTBIOGAS_URL,
            headers=headers,
            params=params,
            timeout=30,
        )

        print(
            f"Smart Biogas HTTP status: "
            f"{response.status_code}"
        )

        response.raise_for_status()
        data = response.json()

    except requests.RequestException as error:
        print(f"Smart Biogas request failed: {error}")
        return None

    except ValueError as error:
        print(f"Invalid Smart Biogas JSON response: {error}")
        return None

    if not isinstance(data, list) or len(data) == 0:
        print(
            "No Smart Biogas records were returned. "
            "Nothing will be uploaded."
        )
        return None

    valid_dictionary_rows = [
        row
        for row in data
        if isinstance(row, dict)
    ]

    if not valid_dictionary_rows:
        print(
            "The API response contains no usable records."
        )
        return None

    # Keep only rows with valid timestamps
    timestamped_rows = []

    for row in valid_dictionary_rows:
        parsed_time = parse_timestamp(
            row.get("timestamp")
        )

        if parsed_time is not None:
            timestamped_rows.append(
                (parsed_time, row)
            )

    if not timestamped_rows:
        print(
            "No records with valid timestamps were returned."
        )
        return None

    # Select the newest record based on its actual datetime
    latest_time, latest_reading = max(
        timestamped_rows,
        key=lambda item: item[0],
    )

    latest_reading["timestamp"] = (
        latest_time.isoformat(timespec="seconds")
    )

    is_valid, reason = validate_reading(
        latest_reading
    )

    if not is_valid:
        print(f"Latest record rejected: {reason}")
        return None

    print(f"Record validation: {reason}")

    return latest_reading


# ============================================================
# 6. READ LAST THINGSPEAK SOURCE TIMESTAMP
# ============================================================

def get_last_thingspeak_status():
    """
    Return the Smart Biogas timestamp stored in the latest
    ThingSpeak status field.

    Duplicate detection is based on timestamp, not sensor values.
    """

    if not THINGSPEAK_CHANNEL_ID:
        print(
            "THINGSPEAK_CHANNEL_ID is not configured. "
            "Duplicate timestamp checking is disabled."
        )
        return None

    url = (
        f"https://api.thingspeak.com/channels/"
        f"{THINGSPEAK_CHANNEL_ID}/feeds/last.json"
    )

    params = {
        "status": "true",
    }

    if THINGSPEAK_READ_KEY:
        params["api_key"] = (
            THINGSPEAK_READ_KEY.strip()
        )

    try:
        response = requests.get(
            url,
            params=params,
            timeout=15,
        )

        if response.status_code != 200:
            print(
                "Could not read the latest ThingSpeak entry: "
                f"{response.text}"
            )
            return None

        latest_entry = response.json()
        last_source_timestamp = latest_entry.get(
            "status"
        )

        return normalize_timestamp(
            last_source_timestamp
        )

    except requests.RequestException as error:
        print(
            "ThingSpeak duplicate-check request failed: "
            f"{error}"
        )
        return None

    except ValueError as error:
        print(
            "ThingSpeak returned invalid JSON: "
            f"{error}"
        )
        return None


# ============================================================
# 7. UPLOAD TO THINGSPEAK
# ============================================================

def upload_to_thingspeak(reading):
    """
    Upload a validated Smart Biogas record to ThingSpeak.
    """

    require_env(
        "THINGSPEAK_WRITE_KEY",
        THINGSPEAK_WRITE_KEY,
    )

    is_valid, validation_message = validate_reading(
        reading
    )

    if not is_valid:
        print(
            f"Upload cancelled: {validation_message}"
        )
        return False

    timestamp = normalize_timestamp(
        reading.get("timestamp")
    )

    flow_lph = reading.get("flowLph")
    volume_l = reading.get("volumeL")
    pressure_pa = reading.get(
        "staticPressurePa"
    )
    battery_v = reading.get("batteryV")
    solar_v = reading.get("solarV")
    rssi_db = reading.get("rssiDb")

    print("-" * 60)
    print("Latest Smart Biogas record:")
    print(f"Timestamp: {timestamp}")
    print(f"Flow: {flow_lph} L/h")
    print(f"Volume: {volume_l} L")
    print(f"Pressure: {pressure_pa} Pa")
    print(f"Battery: {battery_v} V")
    print(f"Solar: {solar_v} V")
    print(f"RSSI: {rssi_db} dBm")

    last_uploaded_timestamp = (
        get_last_thingspeak_status()
    )

    print(
        "Last ThingSpeak source timestamp: "
        f"{last_uploaded_timestamp}"
    )

    # Only matching timestamps are duplicates.
    # Matching measurement values with different timestamps are valid.
    if last_uploaded_timestamp == timestamp:
        print(
            "This exact source record has already been "
            "uploaded. Upload skipped."
        )
        return False

    payload = {
        "api_key": THINGSPEAK_WRITE_KEY.strip(),

        # Only include fields containing valid numeric values.
        "field1": (
            flow_lph
            if is_valid_number(flow_lph)
            else None
        ),
        "field2": (
            volume_l
            if is_valid_number(volume_l)
            else None
        ),
        "field3": (
            pressure_pa
            if is_valid_number(pressure_pa)
            else None
        ),
        "field4": (
            battery_v
            if is_valid_number(battery_v)
            else None
        ),
        "field5": (
            solar_v
            if is_valid_number(solar_v)
            else None
        ),
        "field6": (
            rssi_db
            if is_valid_number(rssi_db)
            else None
        ),

        # Preserve the meter's measurement time.
        "created_at": timestamp,

        # Store the source timestamp for duplicate detection.
        "status": timestamp,
    }

    # Remove None values to prevent null fields from being stored.
    payload = {
        key: value
        for key, value in payload.items()
        if value is not None
    }

    uploaded_measurement_fields = [
        key
        for key in payload
        if key.startswith("field")
    ]

    if not uploaded_measurement_fields:
        print(
            "No valid measurement fields are available. "
            "Nothing will be uploaded."
        )
        return False

    print(
        "Valid fields being uploaded: "
        f"{', '.join(uploaded_measurement_fields)}"
    )

    try:
        response = requests.post(
            THINGSPEAK_UPDATE_URL,
            data=payload,
            timeout=20,
        )

        print(
            f"ThingSpeak HTTP status: "
            f"{response.status_code}"
        )
        print(
            f"ThingSpeak response: {response.text}"
        )

        response.raise_for_status()

    except requests.RequestException as error:
        print(f"ThingSpeak upload failed: {error}")
        return False

    entry_id = response.text.strip()

    if entry_id and entry_id != "0":
        print(
            "Pipeline synchronization completed."
        )
        print(
            f"ThingSpeak entry ID: {entry_id}"
        )
        return True

    print(
        "ThingSpeak rejected the update packet."
    )
    return False


# ============================================================
# 8. MAIN
# ============================================================

def main():
    """
    Execute one synchronization cycle.

    GitHub Actions controls the 20-minute schedule, so no infinite
    loop or sleep function is required here.
    """

    try:
        reading = get_latest_biogas_reading()

        if reading is None:
            print(
                "No suitable new record is available. "
                "Workflow completed without uploading."
            )
            return

        upload_to_thingspeak(reading)

    except Exception as error:
        print(
            f"Synchronization failed: {error}"
        )
        raise


if __name__ == "__main__":
    main()
