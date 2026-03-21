# MUD Client with SSL Support

A Python-based graphical MUD (Multi-User Dungeon) client that connects to MUD servers via SSL/TLS and displays all received text in a user-friendly window.

## Features

- **SSL/TLS Connection**: Securely connects to MUD servers using SSL encryption
- **GUI Interface**: Clean, modern interface built with tkinter
- **Real-time Display**: All text received from the MUD is displayed in real-time
- **Dark Theme**: Easy-on-the-eyes dark color scheme
- **Message Input**: Send commands and messages to the MUD server
- **Connection Management**: Easy connect/disconnect with visual status indicators
- **Error Handling**: Robust error handling for connection issues
- **Thread-safe**: Uses threading for non-blocking network I/O

## Requirements

- Python 3.6 or higher
- tkinter (usually included with Python)
- Standard library modules: `socket`, `ssl`, `threading`, `queue`

## Installation

1. Clone or download this repository
2. Ensure Python 3.6+ is installed:
   ```bash
   python3 --version
   ```

3. No additional packages needed - uses only Python standard library!

## Usage

Run the application:

```bash
python3 mud_client.py
```

Or make it executable:

```bash
chmod +x mud_client.py
./mud_client.py
```

### Connecting to a MUD

1. Enter the MUD server hostname (e.g., `mud.example.com`)
2. Enter the port number (typically 4000, 23, or other MUD-specific port)
3. Click "Connect"
4. Once connected, all text from the MUD will appear in the output window
5. Type commands in the input field and press Enter or click "Send"

### Example MUD Servers

You can test with these public MUD servers (if they support SSL):
- Many MUDs run on standard ports with SSL/TLS support
- Check the specific MUD's documentation for SSL connection details

## Features Explained

### SSL Connection
- Uses Python's `ssl` module with default security settings
- Accepts self-signed certificates (common for MUD servers)
- Automatic hostname verification disabled for flexibility

### GUI Elements
- **Host/Port Fields**: Enter MUD server details
- **Connect/Disconnect Button**: Toggle connection state
- **Status Indicator**: Shows current connection status
- **Output Window**: Displays all MUD text with syntax coloring
- **Input Field**: Send commands to the MUD server
- **Clear Button**: Clear the output window

### Color Coding
- **Cyan**: System messages (connection/disconnection)
- **Yellow**: Your sent messages
- **Red**: Error messages
- **White**: MUD server output

## Troubleshooting

### "Connection refused"
- Check if the server address and port are correct
- Verify the MUD server is running and accepting connections
- Some MUDs may not support SSL on all ports

### "Connection timed out"
- Check your internet connection
- Verify firewall settings aren't blocking the connection
- The server might be down or unreachable

### Unicode/Encoding Issues
- The client tries UTF-8 first, then falls back to Latin-1
- Most modern MUDs use UTF-8 encoding

## Technical Details

- **Threading**: Receive operations run in a separate thread to prevent UI blocking
- **Queue**: Thread-safe queue for communication between network and GUI threads
- **Socket Management**: Proper cleanup of socket resources on disconnect
- **Error Recovery**: Graceful handling of network errors and disconnections

## License

This is free and unencumbered software released via an MIT License.  See the file LICENSE.

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.
