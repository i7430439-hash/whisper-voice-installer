# WhisperVoice Installer

Public installer for WhisperVoice - a push-to-talk voice transcription app for Windows.

## Install (Windows 10/11, 64-bit)

### Step 1 - Download install.bat

Open [install.bat](install.bat) and click the "Download raw file" button (download arrow icon in top-right of the file view).

If the file opens in your browser instead of downloading: press Ctrl+S, save as install.bat (make sure the name ends with .bat, not .txt).

### Step 2 - Run it

Double-click the downloaded install.bat. Follow the on-screen prompts.

The installer will:
- Detect your hardware (NVIDIA GPU + VRAM)
- Recommend a Whisper model based on your hardware
- Download everything it needs
- Create C:\WhisperVoice and a Desktop shortcut

Total install time: 5-15 minutes depending on internet speed and chosen model.

### Step 3 - Use it

Double-click WhisperVoice on your Desktop. The default hotkey is Right Alt (AltGr) - press to start recording, press again to stop and paste the transcribed text.

You can change the hotkey from the tray icon menu (right-click).

## Troubleshooting

If install fails, the log is at %TEMP%\wv-install.log. Send it to whoever shared the installer with you.
