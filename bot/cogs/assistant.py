import os
import pvporcupine
import resampy
import logging
import pyttsx3
import discord
import audioop
import array
import time
import pvrhino
import asyncio

import numpy as np
import speech_recognition as sr

from discord.ext import commands, voice_recv
from discord.ext.commands import Context
from collections import defaultdict, deque

log = logging.getLogger()


class Assistant(commands.Cog):
    def __init__(self, client):
        # Bot state
        self.client = client
        self.recognizer = sr.Recognizer()
        self.enabled = False
        self.always_awake = True

        self._ctx = None
        self._is_awake = False
        self._query = None
        self._transcription_required = False

        # Events
        self._speech_event = asyncio.Event()
        self._intent_event = asyncio.Event()
        self._query_event = asyncio.Event()

        self._loop = asyncio.get_event_loop()
        self._voice_client: discord.VoiceClient = None
        self._resampled_stream = []
        self._stream_data = defaultdict(
            lambda: {"stopper": None, "buffer": array.array("B")}
        )

        # Porcupine wake-word
        self._priority_speaker = None
        self.porcupine = pvporcupine.create(
            access_key=os.getenv("PV_ACCESS_KEY"),
            keyword_paths=["assistant/Okay-Bard_en_windows_v3_0_0.ppn"],
            # keywords=["picovoice", "bumblebee"],
            # sensitivities=[1.0, 1.0],
        )

        # TTS
        self._tts_engine = pyttsx3.init()
        tts_voice_id = self._tts_engine.getProperty("voices")[1].id
        self._tts_engine.setProperty("voice", tts_voice_id)
        self._message_queue = deque()
        self._msg_queue_task = None

        # Rhino speech-to-intent
        self._intent_queue = deque()
        self._loop.create_task(self._process_intent())
        self.rhino = pvrhino.create(
            access_key=os.getenv("PV_ACCESS_KEY"),
            context_path="assistant/Bard-Assistant_en_windows_v3_0_0.rhn",
            # require_endpoint=False,  # Rhino will not require an chunk of silence at the end
        )

    def _detect_intent(self, audio_frame):
        """
        Detects intent. Inferred intent is pushed to the intent
        queue to be processed.
        """

        # Determine intent from speech
        is_finalized = self.rhino.process(audio_frame)
        if is_finalized:
            inference = self.rhino.get_inference()

            if inference.is_understood:
                self._is_awake = False

                # Add intent to queue and set event for processing
                log.info(inference.intent)
                self._intent_queue.append(inference)
                self._loop.call_soon_threadsafe(self._intent_event.set)

    def say(self, msg):
        """
        Adds a message to the assistant's message queue which will be
        played over the bot's voice client connection using TTS along with
        a text message.

        Audio for the message will only be played if there is no audio
        currently playing. Otherwise, only a text message will be displayed

        Messages will not play over each other and can only played if
        a coroutine to process them exists. In other words, the assistant
        should be enabled for the assistant to broadcast messages.
        """

        self._message_queue.append(msg)

        # Modify the event from another thread using call_soon_threadsafe
        # Reference: https://stackoverflow.com/questions/64651519/how-to-pass-an-event-into-an-async-task-from-another-thread
        self._loop.call_soon_threadsafe(self._speech_event.set)

    async def _process_message_queue(self):
        """
        Starts a repeating coroutine service that generates speech for
        messages added to message_queue. There should be only one instance
        of this task running.
        """

        while True:
            # Wait for a message event
            await self._speech_event.wait()

            if len(self._message_queue) == 0:
                return

            message = self._message_queue.popleft()
            self._tts_engine.save_to_file(message, "assistant/reply.wav")
            self._tts_engine.runAndWait()

            audio = await discord.FFmpegOpusAudio.from_probe("assistant/reply.wav")
            if not self._voice_client.is_playing():
                self._voice_client.play(audio)
            await self._ctx.send(message)

            self._speech_event.clear()

    async def _process_intent(self):
        """
        Starts a repeating coroutine to process intents one at a time.
        """

        while True:
            # Wait for intent
            await self._intent_event.wait()

            if len(self._intent_queue) == 0:
                return

            inference = self._intent_queue.popleft()
            command = self.client.get_command(inference.intent)

            if command:
                log.info(f"Executing intent: {command}")

                if inference.intent == "play":
                    # Additional transcription is required

                    # TODO: Refactor into own method
                    self._query = None
                    self._stream_data[self._priority_speaker.id] = {
                        "stopper": None,
                        "buffer": array.array("B"),
                    }
                    self._transcription_required = True
                    self.say("What would you like me to play?")

                    await self._query_event.wait()

                    # Stop background whisper transcriber and delete stopper callback
                    stopper_cb = self._stream_data[self._priority_speaker.id]["stopper"]
                    stopper_cb(False)

                    await command(self._ctx, query=self._query)
                    self._query_event.clear()
                    self._transcription_required = False

                else:
                    self.say(f"Okay, I will {inference.intent}")
                    await command(self._ctx)

            self._intent_event.clear()

    def enable(self, ctx: Context):
        """
        Enables the assistant. When in listening mode, the assistant waits
        for the wake word to trigger command mode. In command mode, the
        assistant will interpret the intent of the speaker who woke the assistant
        """

        if self.enabled:
            log.info("Assistant is already enabled")
            return

        self.enabled = True
        self._ctx = ctx
        self._voice_client = ctx.voice_client
        self._priority_speaker = ctx.author

        el = asyncio.get_event_loop()
        self._msg_queue_task = el.create_task(self._process_message_queue())

        def callback(user: discord.User, data: voice_recv.VoiceData):
            # Only process packets from the priority speaker
            if user is None or user.id != self._priority_speaker.id:
                return

            """
            Porcupine expects a frame of 512 samples.

            Each data.pcm array has 3840 bytes. This is because Discord
            is streaming stereo audio at a samplerate of 48kHz at 16 bits.
            The value of 3840 is calculated in the following manner
                (48000 samples * 0.02 seconds * 16 bit depth * 2 channels) / 8
                    = 3840 bytes

            In this case, the bytes of the PCM stream are stored in the format
            below:
                L L R R L L R R L L R R L L R R

            Here, each individual letter represents a single byte. Since each
            sample is measured with 16 bits, two bytes are needed for each
            sample. Additionally, because this PCM stream is stereo (has 2
            channels), the samples of both channels are interleaved such that
            the first sample belongs to the left channel followed by a sample
            belonging to the right and so on.

            Reference:
            https://stackoverflow.com/questions/32128206/what-does-interleaved-stereo-pcm-linear-int16-big-endian-audio-look-like
            """

            if self._transcription_required:
                # Additional speech transcription is required

                sdata = self._stream_data[user.id]
                sdata["buffer"].extend(data.pcm)

                if not sdata["stopper"]:
                    sdata["stopper"] = self.recognizer.listen_in_background(
                        DiscordSRAudioSource(sdata["buffer"]),
                        self.get_bg_listener_callback(user),
                        phrase_time_limit=10,
                    )
            else:
                # Direct all PCM packets to porcupine or rhino if
                # transcription is not required

                # The PCM stream from Discord arrives in bytes in Little Endian format
                values = np.frombuffer(data.pcm, dtype=np.int16)
                value_matrix = np.array((values[::2], values[1::2]))

                # Downsample the audio stream from 48kHz to 16kHz
                resampled_values = resampy.resample(
                    value_matrix, 48_000, 16_000
                ).astype(value_matrix.dtype)

                # Extend the buffer with the samples for the current user collected
                # at this instance and choose the left channel only
                self._resampled_stream.extend(resampled_values[0])
                resampled_buffer = self._resampled_stream

                # log.info(self._resampled_stream)

                if len(resampled_buffer) >= 512:
                    # Buffer has >512 bytes
                    audio_frame = resampled_buffer[:512]
                    self._resampled_stream = resampled_buffer[512:]

                    if self.always_awake or self._is_awake:
                        # Determine intent from speech
                        self._detect_intent(audio_frame)
                    else:
                        # Listen for wake word
                        result = self.porcupine.process(audio_frame)
                        log.info(result)

                        if result >= 0:
                            # Set awake
                            self._is_awake = True

                            log.info("Detected wake word")
                            self.say(f"Hi {user.display_name}, how can I help you?")

        assistant_sink = voice_recv.SilenceGeneratorSink(voice_recv.BasicSink(callback))
        ctx.voice_client.listen(assistant_sink)

    def disable(self, ctx: Context):
        """
        Disables the assistant and cancels any connections and related tasks
        """

        self.enabled = False
        self._ctx = ctx
        ctx.voice_client.stop_listening()

        # Clean up
        self._msg_queue_task.cancel()
        self._speech_event.clear()
        self._intent_event.clear()
        self._message_queue.clear()
        self._intent_queue.clear()

    def get_bg_listener_callback(self, user: discord.User):
        def callback(recognizer: sr.Recognizer, audio):
            if self._query == None:
                self._query = recognizer.recognize_whisper(
                    audio, model="small", language="english"
                )
                self._loop.call_soon_threadsafe(self._query_event.set)
                print(f'{user.display_name} said "{self._query}"')

        return callback


class DiscordSRAudioSource(sr.AudioSource):
    little_endian = True
    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    CHANNELS = 2
    CHUNK = 960

    def __init__(self, buffer: array.array[int]):
        self.buffer = buffer
        self._entered: bool = False

    @property
    def stream(self):
        return self

    def __enter__(self):
        if self._entered:
            log.warning("Already entered sr audio source")
        self._entered = True
        return self

    def __exit__(self, *exc) -> None:
        self._entered = False
        if any(exc):
            log.exception("Error closing sr audio source")

    def read(self, size: int) -> bytes:
        # TODO: make this timeout configurable
        for _ in range(10):
            if len(self.buffer) < size * self.CHANNELS:
                time.sleep(0.1)
            else:
                break
        else:
            if len(self.buffer) == 0:
                return b""

        chunksize = size * self.CHANNELS
        audiochunk = self.buffer[:chunksize].tobytes()
        del self.buffer[: min(chunksize, len(audiochunk))]
        audiochunk = audioop.tomono(audiochunk, 2, 1, 1)
        return audiochunk

    def close(self) -> None:
        self.buffer.clear()


async def setup(client):
    await client.add_cog(Assistant(client))
