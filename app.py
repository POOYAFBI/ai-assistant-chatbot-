import os
import tempfile
import threading
import subprocess
import uuid
import speech_recognition as sr
import openai
import time
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from elevenlabs import save
from elevenlabs.client import ElevenLabs
from httpx import ConnectTimeout, HTTPStatusError
from datetime import datetime, timedelta

app = Flask(__name__, template_folder='template')
CORS(app)

openai.api_key = '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'
elevenlabs_api_key = 'sk_5ef69028ca0c306f8186d8621f3ab1a06d1763befa002157'
client = ElevenLabs(api_key=elevenlabs_api_key)

MAX_TOKENS_PER_DAY = 20
user_sessions = {}

def reset_daily_tokens(user_id):
    user_sessions[user_id] = {
        'tokens': MAX_TOKENS_PER_DAY,
        'last_reset': datetime.now()
    }

def check_and_update_tokens(user_id):
    session = user_sessions.get(user_id)
    
    if session:
        if datetime.now() - session['last_reset'] > timedelta(days=1):
            reset_daily_tokens(user_id)
    else:
        reset_daily_tokens(user_id)

    return user_sessions[user_id]['tokens'] > 0

def use_token(user_id):
    if user_sessions[user_id]['tokens'] > 0:
        user_sessions[user_id]['tokens'] -= 1

def convert_webm_to_wav(webm_file_path):
    wav_file_path = webm_file_path.replace('.webm', '.wav')
    try:
        subprocess.run(
            ['ffmpeg', '-i', webm_file_path, wav_file_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return wav_file_path
    except subprocess.CalledProcessError as e:
        print(f"Error converting file: {e.stderr.decode()}")
        raise

def speech_to_text(audio_data_path):
    r = sr.Recognizer()
    try:
        with sr.AudioFile(audio_data_path) as source:
            audio = r.record(source)
        text = r.recognize_google(audio)
        return text
    except sr.UnknownValueError:
        return "Google Speech Recognition could not understand audio"
    except sr.RequestError as e:
        return f"Could not request results from Google Speech Recognition service; {e}"

conversation_history = []

def chat_with_gpt(prompt):
    global conversation_history
    
    conversation_history.append({"role": "user", "content": prompt})
    
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a professional accountant and tax consultant designed to assist users in managing their company's financials. Your task is to collect relevant financial data from the user, perform accurate calculations, and offer useful insights and recommendations regarding tax strategies, financial planning, and cost management. You aim to simplify complex financial concepts and ensure that users make informed, efficient decisions for their business."}
        ] + conversation_history,
        max_tokens=100
    )
    
    response_text = response['choices'][0]['message']['content'].strip()
    
    conversation_history.append({"role": "assistant", "content": response_text})
    
    if len(conversation_history) > 10:
        conversation_history.pop(0)
        conversation_history.pop(0)

    return response_text


def text_to_speech_with_retry(text, retries=3, delay=5):
    file_name = f"response_{uuid.uuid4().hex}.mp3"
    file_path = os.path.join('static', file_name)
    for attempt in range(retries):
        try:
            audio = client.generate(
                text=text,
                voice="Jessica"
            )
            save(audio, file_path)
            return file_name
        except (ConnectTimeout, HTTPStatusError) as e:
            print(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay} seconds...")
            time.sleep(delay)
    raise Exception("All attempts to connect to ElevenLabs API failed.")

def process_audio_thread(audio_file, user_id):
    with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as temp_file:
        temp_file.write(audio_file.read())
        temp_file_path = temp_file.name

    wav_temp_path = convert_webm_to_wav(temp_file_path)
    
    text = speech_to_text(wav_temp_path)
    if not text:
        os.remove(temp_file_path)
        os.remove(wav_temp_path)
        return {"error": "Speech recognition failed"}

    response_text = chat_with_gpt(text)
    audio_response = text_to_speech_with_retry(response_text)
    
    os.remove(temp_file_path)
    os.remove(wav_temp_path)
    return {"response_text": response_text, "audio_file": audio_response}

def process_text_thread(text, user_id):
    response_text = chat_with_gpt(text)
    audio_response = text_to_speech_with_retry(response_text)
    return {"response_text": response_text, "audio_file": audio_response}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process_audio', methods=['POST'])
def process_audio():
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file found"}), 400
    
    user_id = request.remote_addr
    audio_file = request.files['audio']
    result = {}

    check_and_update_tokens(user_id)
    if not check_and_update_tokens(user_id):
        return jsonify({"error": "حداکثر مقدار مصرف روزانه شما به پایان رسیده است."}), 403
    
    use_token(user_id)

    try:
        def audio_processing():
            nonlocal result
            result = process_audio_thread(audio_file, user_id)
        
        audio_thread = threading.Thread(target=audio_processing)
        audio_thread.start()
        audio_thread.join()

        if 'error' in result:
            return jsonify({"error": result["error"]}), 500

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/process_text', methods=['POST'])
def process_text():
    data = request.json
    if not data or 'text' not in data:
        return jsonify({"error": "No text provided"}), 400
    
    user_id = request.remote_addr
    text = data['text']
    result = {}

    check_and_update_tokens(user_id)
    if not check_and_update_tokens(user_id):
        return jsonify({"error": "حداکثر مقدار مصرف روزانه شما به پایان رسیده است."}), 403

    use_token(user_id)

    def text_processing():
        nonlocal result
        result = process_text_thread(text, user_id)
    
    text_thread = threading.Thread(target=text_processing)
    text_thread.start()
    text_thread.join()
    
    return jsonify(result)

@app.route('/static/<filename>', methods=['GET'])
def get_audio(filename):
    file_path = os.path.join('static', filename)
    if os.path.exists(file_path):
        return app.send_static_file(filename)
    else:
        return jsonify({"error": "File not found"}), 404

@app.route('/delete_audio/<filename>', methods=['DELETE'])
def delete_audio(filename):
    file_path = os.path.join('static', filename)
    try:
        os.remove(file_path)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, threaded=True)
