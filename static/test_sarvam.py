import os

import requests
API_KEY = os.getenv("API_KEY", "")
API_URL = "https://api.sarvam.ai/text-to-speech/stream"
def stream_tts():
    headers = {
        "api-subscription-key": API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "text": """Hello! This is a streaming text-to-speech example.""",
        "target_language_code": "hi-IN",
        "speaker": "shubh",
        "model": "bulbul:v3",
        "pace": 1.1,
        "speech_sample_rate": 22050,
        "output_audio_codec": "mp3",
        "enable_preprocessing": True
    }

    # Stream the response
    with requests.post(API_URL, headers=headers, json=payload, stream=True) as response:
        response.raise_for_status()

        # Save to file as chunks arrive
        with open("output.mp3", "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    print(f"Received {len(chunk)} bytes")

        print("Audio saved to output.mp3")
if __name__ == "__main__":
    stream_tts()