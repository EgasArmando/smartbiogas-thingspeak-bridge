import os
import requests
from datetime import datetime, timedelta, timezone

SMARTBIOGAS_API_KEY = os.getenv("SMARTBIOGAS_API_KEY")
THINGSPEAK_WRITE_KEY = os.getenv("THINGSPEAK_WRITE_KEY")
THINGSPEAK_CHANNEL_ID = os.getenv("THINGSPEAK_CHANNEL_ID", "")
THINGSPEAK_READ_KEY = os.getenv("THINGSPEAK_READ_KEY", "")

METER_ID = os.getenv("METER_ID", "mg6gx43")

SMARTBIOGAS_URL = "https://api.smartbiogas.io/api/v1/gas-meter-reports"
THINGSPEAK_UPDATE_URL = "https://api.thingspeak.com/update"


def require_env(name, value):
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")


def get_latest_biogas_reading():
    require_env("SMARTBIOGAS_API_KEY", SMARTBIOGAS_API_KEY)

    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=1)

    params = {
        "gasMeterId": METER_ID,
        "startAt": start_at.isoformat(),
        "endAt": end_at.isoformat(),
    }

    headers = {
        "api-key": SMARTBIOGAS_API_KEY.strip(),
        "Accept": "application/json",
    }

    print(f"Connecting to Smart Biogas API for meter: {METER_ID}")

    response = requests.get(
        SMARTBIOGAS_URL,
        headers=headers,
        params=params,
        timeout=30,
    )

    print(f"Smart Biogas HTTP Status: {response.status_code}")

    if response.status_code != 200:
        print(response.text)
        return None

    data = response.json()

    if not isinstance(data, list) or len(data) == 0:
        print("No Smart Biogas readings returned.")
        return None

    data = sorted(data, key=lambda row: row.get("timestamp", ""))
    return data[-1]


def get_last_thingspeak_status():
    """
    Optional duplicate protection.

    If THINGSPEAK_CHANNEL_ID is provided, the script reads the latest ThingSpeak
    entry and checks its status field. We store the Smart Biogas timestamp in
    ThingSpeak status so the next run can avoid uploading the same reading again.
    """

    if not THINGSPEAK_CHANNEL_ID:
        return None

    url = f"https://api.thingspeak.com/channels/{THINGSPEAK_CHANNEL_ID}/feeds/last.json"

    params = {
        "status": "true",
    }

    if THINGSPEAK_READ_KEY:
        params["api_key"] = THINGSPEAK_READ_KEY.strip()

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code != 200:
            print(f"Could not read latest ThingSpeak entry: {response.text}")
            return None

        latest = response.json()
        return latest.get("status")

    except Exception as error:
        print(f"ThingSpeak duplicate-check warning: {error}")
        return None


def upload_to_thingspeak(reading):
    require_env("THINGSPEAK_WRITE_KEY", THINGSPEAK_WRITE_KEY)

    timestamp = reading.get("timestamp")

    flow_lph = reading.get("flowLph")
    volume_l = reading.get("volumeL")
    pressure_pa = reading.get("staticPressurePa")
    battery_v = reading.get("batteryV")
    solar_v = reading.get("solarV")
    rssi_db = reading.get("rssiDb")

    print("Latest Smart Biogas reading:")
    print(f"Timestamp: {timestamp}")
    print(f"Flow: {flow_lph} L/h")
    print(f"Volume: {volume_l} L")
    print(f"Pressure: {pressure_pa} Pa")
    print(f"Battery: {battery_v} V")
    print(f"Solar: {solar_v} V")
    print(f"RSSI: {rssi_db} dBm")

    last_status = get_last_thingspeak_status()

    if last_status == timestamp:
        print(f"No new reading. Already uploaded timestamp: {timestamp}")
        return

    payload = {
        "api_key": THINGSPEAK_WRITE_KEY.strip(),
        "field1": flow_lph,
        "field2": volume_l,
        "field3": pressure_pa,
        "field4": battery_v,
        "field5": solar_v,
        "field6": rssi_db,
        "status": timestamp,
    }

    payload = {key: value for key, value in payload.items() if value is not None}

    print("Uploading to ThingSpeak...")

    response = requests.post(
        THINGSPEAK_UPDATE_URL,
        data=payload,
        timeout=20,
    )

    print(f"ThingSpeak HTTP Status: {response.status_code}")
    print(f"ThingSpeak response: {response.text}")

    if response.status_code == 200 and response.text.strip() != "0":
        print(f"Pipeline Sync Complete. ThingSpeak Entry ID: {response.text}")
    else:
        print("ThingSpeak rejected the update packet.")


def main():
    reading = get_latest_biogas_reading()

    if reading is None:
        return

    upload_to_thingspeak(reading)


if __name__ == "__main__":
    main()
