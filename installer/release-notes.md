## Set up your WeatherPi

You need: a Windows PC, the WeatherPi, a micro-USB **data** cable, an SD card (8 GB or more) and a card reader, and your WiFi password.

1. **Download `WeatherPi-Companion-Setup-{VERSION}.exe`** below (under *Assets*) and run it. Windows may warn that the app isn't recognised, because it isn't signed yet: click **More info**, then **Run anyway**. It installs the WeatherPi Companion app, the WeatherPi's USB driver and Tailscale.
2. **Open WeatherPi Companion** and choose **Flash a new SD card**. The app downloads the WeatherPi software (`weatherpi-….img.xz`, also below) and writes it to the card by itself; you don't need to download it.
3. **Put the card in the WeatherPi and plug its USB port into the PC** with the data cable. That cable powers it too: **don't plug anything into PWR at the same time**, power from both ports at once can destroy the WeatherPi.
4. **Follow the steps** in the app: a password, the WiFi, and Tailscale (a free account) so you can see the station from anywhere. The first start takes a few minutes, and the WeatherPi restarts once by itself.
5. When it says it's ready, unplug the USB cable, then power the WeatherPi from its PWR port.

Stuck? Click **Need help?** in the app, then **Create help text** and **Copy to clipboard**, and paste it into an AI chat (ChatGPT, Claude, Gemini, ...). Passwords are left out.

## Files

- `WeatherPi-Companion-Setup-{VERSION}.exe`: the Windows app and everything the PC needs.
- `weatherpi-….img.xz`: the SD card image (the app downloads it for you). Each file has a `.sha256` checksum next to it.
