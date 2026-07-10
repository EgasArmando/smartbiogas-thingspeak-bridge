import os
import math
import requests
from datetime import datetime, timedelta, timezone

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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

THINGSPEAK_UPDATE_URL = (
    "https://api.thingspeak.com/update"
)

# Search Smart Biogas records from the previous 24 hours.
LOOKBACK_HOURS = 24

# Reject records older than 60 minutes.
# The GitHub workflow runs every 20 minutes, so this allows
# some tolerance for delayed sensor updates or workflow starts.
MAX_RECORD_AGE_MINUTES = 60

# Reject timestamps that are more than five minutes in the future.
MAX_FUTURE_TIME_MINUTES = 5

# At least one valid measurement must be present.
MINIMUM_VALID_FIELDS = 1


# ============================================================
# 3. HTTP SESSION WITH RETRIES
# ============================================================

def create_http_session():
    """
    Create a reusable HTTP session with automatic retries for
    temporary server and network failures.
    """

    retry_strategy = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=2,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=[
            "GET",
            "POST",
        ],
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


HTTP_SESSION = create_http_session()


# ============================================================
# 4. GENERAL SUPPORT FUNCTIONS
# ============================================================

def require_env(name, value):
    """
    Ensure that a required environment variable exists.
    """

    if value is None or not str(value).strip():
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )


def utc_now():
    """
    Return the current timezone-aware UTC datetime.
    """

    return datetime.now(timezone.utc)


def parse_timestamp(value):
    """
    Parse an ISO 8601 timestamp and return a timezone-aware
    UTC datetime.

    Examples:
        2026-07-10T10:40:00Z
        2026-07-10T10:40:00+00:00
        2026-07-10T10:40:00
    """

    if value is None:
        return None

    timestamp_text = str(value).strip()

    if not timestamp_text:
        return None

    try:
        timestamp_text = timestamp_text.replace(
            "Z",
            "+00:00",
        )

        parsed = datetime.fromisoformat(
            timestamp_text
        )

        # If the API gives no timezone, assume UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except (ValueError, TypeError):
        return None


def normalize_timestamp(value):
    """
    Convert a timestamp to a consistent UTC ISO 8601 string.
    """

    parsed = parse_timestamp(value)

    if parsed is None:
        return None

    return parsed.isoformat(
        timespec="seconds"
    )


def is_valid_number(value):
    """
    Return True when a value is a valid finite number.

    Important:
        0 is valid.
        None is invalid.
        Empty text is invalid.
        NaN is invalid.
        Infinity is invalid.
        Boolean values are invalid.
    """

    if value is None:
        return False

    if isinstance(value, bool):
        return False

    if (
        isinstance(value, str)
        and not value.strip()
    ):
        return False

    try:
        numeric_value = float(value)

        return math.isfinite(
            numeric_value
        )

    except (TypeError, ValueError):
        return False


def get_measurement_fields(reading):
    """
    Map Smart Biogas measurements to ThingSpeak fields.
    """

    return {
        "field1": reading.get("flowLph"),
        "field2": reading.get("volumeL"),
        "field3": reading.get(
            "staticPressurePa"
        ),
        "field4": reading.get("batteryV"),
        "field5": reading.get("solarV"),
        "field6": reading.get("rssiDb"),
    }


def count_valid_measurements(reading):
    """
    Count valid measurements in a Smart Biogas record.
    """

    fields = get_measurement_fields(
        reading
    )

    return sum(
        is_valid_number(value)
        for value in fields.values()
    )


# ============================================================
# 5. RECORD VALIDATION
# ============================================================

def validate_reading(reading):
    """
    Validate a Smart Biogas record.

    Identical measurements at different timestamps are valid.
    Duplicate detection is based only on the source timestamp.
    """

    if not isinstance(reading, dict):
        return (
            False,
            "The Smart Biogas record is invalid.",
        )

    record_time = parse_timestamp(
        reading.get("timestamp")
    )

    if record_time is None:
        return (
            False,
            "The record does not contain a valid timestamp.",
        )

    current_time = utc_now()
    record_age = current_time - record_time

    if record_age < timedelta(
        minutes=-MAX_FUTURE_TIME_MINUTES
    ):
        return (
            False,
            (
                "The record timestamp is unexpectedly "
                f"in the future: "
                f"{normalize_timestamp(record_time)}"
            ),
        )

    if record_age > timedelta(
        minutes=MAX_RECORD_AGE_MINUTES
    ):
        age_minutes = (
            record_age.total_seconds() / 60
        )

        return (
            False,
            (
                f"The newest usable record is "
                f"{age_minutes:.1f} minutes old. "
                "The meter may be offline."
            ),
        )

    valid_measurement_count = (
        count_valid_measurements(reading)
    )

    if (
        valid_measurement_count
        < MINIMUM_VALID_FIELDS
    ):
        return (
            False,
            (
                "The record contains no valid "
                "sensor measurements."
            ),
        )

    return (
        True,
        (
            f"Record is valid with "
            f"{valid_measurement_count} valid "
            "measurement field(s)."
        ),
    )


# ============================================================
# 6. SMART BIOGAS API
# ============================================================

def get_latest_biogas_reading():
    """
    Retrieve Smart Biogas records and select the newest record
    containing:
        - a valid timestamp
        - at least one valid sensor measurement
    """

    require_env(
        "SMARTBIOGAS_API_KEY",
        SMARTBIOGAS_API_KEY,
    )

    end_at = utc_now()

    start_at = end_at - timedelta(
        hours=LOOKBACK_HOURS
    )

    params = {
        "gasMeterId": METER_ID,
        "startAt": start_at.isoformat(
            timespec="seconds"
        ),
        "endAt": end_at.isoformat(
            timespec="seconds"
        ),
    }

    headers = {
        "api-key": SMARTBIOGAS_API_KEY.strip(),
        "Accept": "application/json",
    }

    print("-" * 70)
    print("CONNECTING TO SMART BIOGAS")
    print("-" * 70)
    print(f"Meter ID: {METER_ID}")
    print(
        "Search start UTC: "
        f"{start_at.isoformat(timespec='seconds')}"
    )
    print(
        "Search end UTC: "
        f"{end_at.isoformat(timespec='seconds')}"
    )

    try:
        response = HTTP_SESSION.get(
            SMARTBIOGAS_URL,
            headers=headers,
            params=params,
            timeout=30,
        )

        print(
            "Smart Biogas HTTP status: "
            f"{response.status_code}"
        )

        response.raise_for_status()

        data = response.json()

    except requests.RequestException as error:
        print(
            f"Smart Biogas request failed: {error}"
        )
        return None

    except ValueError as error:
        print(
            "Smart Biogas returned invalid JSON: "
            f"{error}"
        )
        return None

    if not isinstance(data, list):
        print(
            "Unexpected Smart Biogas response format. "
            "A list of records was expected."
        )
        return None

    if not data:
        print(
            "No Smart Biogas records were returned. "
            "Nothing will be uploaded."
        )
        return None

    print(
        f"Smart Biogas records returned: {len(data)}"
    )

    usable_records = []

    for record in data:
        if not isinstance(record, dict):
            continue

        record_time = parse_timestamp(
            record.get("timestamp")
        )

        if record_time is None:
            continue

        valid_measurement_count = (
            count_valid_measurements(record)
        )

        # Ignore records containing only null, empty,
        # NaN or otherwise invalid measurements.
        if (
            valid_measurement_count
            < MINIMUM_VALID_FIELDS
        ):
            continue

        usable_records.append(
            (
                record_time,
                record,
                valid_measurement_count,
            )
        )

    if not usable_records:
        print(
            "No Smart Biogas records contain both "
            "a valid timestamp and a valid measurement."
        )
        return None

    (
        record_time,
        latest_record,
        valid_count,
    ) = max(
        usable_records,
        key=lambda item: item[0],
    )

    latest_record["timestamp"] = (
        record_time.isoformat(
            timespec="seconds"
        )
    )

    print(
        "Newest usable source timestamp: "
        f"{latest_record['timestamp']}"
    )
    print(
        "Valid measurements in newest record: "
        f"{valid_count}"
    )

    valid, reason = validate_reading(
        latest_record
    )

    if not valid:
        print(
            f"Record rejected: {reason}"
        )
        return None

    print(
        f"Record validation: {reason}"
    )

    return latest_record


# ============================================================
# 7. READ LAST THINGSPEAK RECORD
# ============================================================

def get_last_thingspeak_timestamp():
    """
    Read the Smart Biogas source timestamp stored in the
    status field of the latest ThingSpeak entry.
    """

    if not THINGSPEAK_CHANNEL_ID:
        print(
            "THINGSPEAK_CHANNEL_ID is missing. "
            "Duplicate and older-record protection "
            "cannot run."
        )
        return None

    url = (
        "https://api.thingspeak.com/channels/"
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
        response = HTTP_SESSION.get(
            url,
            params=params,
            timeout=15,
        )

        print(
            "ThingSpeak read HTTP status: "
            f"{response.status_code}"
        )

        if response.status_code != 200:
            print(
                "Could not read the latest "
                "ThingSpeak entry: "
                f"{response.text}"
            )
            return None

        latest_entry = response.json()

        status_timestamp = (
            latest_entry.get("status")
        )

        return normalize_timestamp(
            status_timestamp
        )

    except requests.RequestException as error:
        print(
            "ThingSpeak duplicate-check request "
            f"failed: {error}"
        )
        return None

    except ValueError as error:
        print(
            "ThingSpeak returned invalid JSON "
            "during duplicate checking: "
            f"{error}"
        )
        return None


# ============================================================
# 8. UPLOAD TO THINGSPEAK
# ============================================================

def upload_to_thingspeak(reading):
    """
    Upload one valid and newer Smart Biogas record.

    A record is skipped when:
        - the timestamp is missing
        - all measurements are invalid
        - it has the same timestamp as the last entry
        - it is older than the last ThingSpeak source record
        - it is stale relative to the current UTC time
    """

    require_env(
        "THINGSPEAK_WRITE_KEY",
        THINGSPEAK_WRITE_KEY,
    )

    valid, reason = validate_reading(
        reading
    )

    if not valid:
        print(
            f"Upload cancelled: {reason}"
        )
        return False

    current_timestamp = normalize_timestamp(
        reading.get("timestamp")
    )

    current_record_time = parse_timestamp(
        current_timestamp
    )

    if current_record_time is None:
        print(
            "Upload cancelled because the source "
            "timestamp could not be parsed."
        )
        return False

    print("-" * 70)
    print("LATEST SMART BIOGAS RECORD")
    print("-" * 70)
    print(
        f"Timestamp: {current_timestamp}"
    )
    print(
        f"Flow: {reading.get('flowLph')} L/h"
    )
    print(
        f"Volume: {reading.get('volumeL')} L"
    )
    print(
        "Pressure: "
        f"{reading.get('staticPressurePa')} Pa"
    )
    print(
        f"Battery: {reading.get('batteryV')} V"
    )
    print(
        f"Solar: {reading.get('solarV')} V"
    )
    print(
        f"RSSI: {reading.get('rssiDb')} dBm"
    )

    last_uploaded_timestamp = (
        get_last_thingspeak_timestamp()
    )

    last_uploaded_time = parse_timestamp(
        last_uploaded_timestamp
    )

    print(
        "Current Smart Biogas timestamp: "
        f"{current_timestamp}"
    )
    print(
        "Last ThingSpeak source timestamp: "
        f"{last_uploaded_timestamp}"
    )

    # Skip the same timestamp and any older timestamp.
    # Measurement values are not used for duplicate detection.
    if (
        last_uploaded_time is not None
        and current_record_time
        <= last_uploaded_time
    ):
        if (
            current_record_time
            == last_uploaded_time
        ):
            print(
                "This exact Smart Biogas record has "
                "already been uploaded."
            )
        else:
            print(
                "The Smart Biogas API returned a "
                "record older than the latest record "
                "already stored in ThingSpeak."
            )

        print("Upload skipped.")
        return False

    all_fields = get_measurement_fields(
        reading
    )

    valid_fields = {
        field_name: value
        for field_name, value
        in all_fields.items()
        if is_valid_number(value)
    }

    if not valid_fields:
        print(
            "No valid sensor measurements are "
            "available. Upload skipped."
        )
        return False

    payload = {
        "api_key": (
            THINGSPEAK_WRITE_KEY.strip()
        ),

        # Preserve the Smart Biogas source time.
        "created_at": current_timestamp,

        # Store source timestamp for duplicate and
        # chronological-order checking.
        "status": current_timestamp,

        **valid_fields,
    }

    print("-" * 70)
    print("UPLOADING TO THINGSPEAK")
    print("-" * 70)
    print(
        "Valid fields being uploaded: "
        f"{', '.join(valid_fields.keys())}"
    )
    print(
        "Source measurement time: "
        f"{current_timestamp}"
    )
    print(
        "Upload execution time UTC: "
        f"{utc_now().isoformat(timespec='seconds')}"
    )

    try:
        response = HTTP_SESSION.post(
            THINGSPEAK_UPDATE_URL,
            data=payload,
            timeout=20,
        )

        print(
            "ThingSpeak write HTTP status: "
            f"{response.status_code}"
        )
        print(
            f"ThingSpeak response: {response.text}"
        )

        response.raise_for_status()

    except requests.RequestException as error:
        print(
            f"ThingSpeak upload failed: {error}"
        )
        return False

    entry_id = response.text.strip()

    if entry_id and entry_id != "0":
        print(
            "Pipeline synchronization completed "
            "successfully."
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
# 9. MAIN EXECUTION
# ============================================================

def main():
    """
    Run one synchronization cycle.

    GitHub Actions controls the 20-minute schedule.
    This script runs once and exits.
    """

    print("=" * 70)
    print("SMART BIOGAS SYNCHRONIZATION")
    print("=" * 70)
    print(
        "Execution UTC: "
        f"{utc_now().isoformat(timespec='seconds')}"
    )
    print("=" * 70)

    try:
        reading = get_latest_biogas_reading()

        if reading is None:
            print("-" * 70)
            print(
                "No suitable new Smart Biogas "
                "record is available."
            )
            print(
                "Workflow completed without creating "
                "a ThingSpeak entry."
            )
            return

        uploaded = upload_to_thingspeak(
            reading
        )

        if uploaded:
            print("-" * 70)
            print(
                "A new Smart Biogas record was "
                "uploaded successfully."
            )
        else:
            print("-" * 70)
            print(
                "No new ThingSpeak entry was created."
            )

    except Exception as error:
        print("-" * 70)
        print(
            f"Synchronization failed: {error}"
        )

        # Raise the error so GitHub Actions marks
        # the workflow as failed.
        raise

    finally:
        print("=" * 70)
        print("Synchronization finished.")
        print(
            "Finish UTC: "
            f"{utc_now().isoformat(timespec='seconds')}"
        )
        print("=" * 70)


if __name__ == "__main__":
    main()
