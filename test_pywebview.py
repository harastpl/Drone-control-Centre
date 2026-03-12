import sys
import webview

print("=" * 50)
print("PYWEBVIEW TEST")
print("=" * 50)

print(f"Python version: {sys.version}")
print(f"Python executable: {sys.executable}")
print(f"Platform: {sys.platform}")

# Check webview module info
print(f"\nWebview module location: {webview.__file__}")

# List available attributes
print("\nAvailable webview attributes:")
webview_attrs = [attr for attr in dir(webview) if not attr.startswith('_')]
print(f"  {', '.join(webview_attrs[:10])}...")  # Show first 10

# Try a super simple window
print("\n" + "=" * 50)
print("ATTEMPTING TO CREATE WINDOW")
print("=" * 50)

try:
    # Simple HTML content
    html = """
    <html>
    <body style="background: #f0f0f0; font-family: Arial; text-align: center; padding: 50px;">
        <h1 style="color: #333;">Test Window</h1>
        <p style="color: #666;">If you can see this, pywebview is working!</p>
    </body>
    </html>
    """
    
    # Create window with minimal parameters
    window = webview.create_window(
        'Test Window',
        html=html,
        width=400,
        height=300
    )
    
    print("✓ Window object created successfully")
    print("Starting webview (window should appear)...")
    print("(If no window appears within 10 seconds, there might be an issue)")
    
    # Start webview with timeout
    import threading
    import time
    
    def timeout_handler():
        time.sleep(10)
        print("⚠️  No window appeared after 10 seconds")
        print("Attempting to force quit...")
        sys.exit(1)
    
    # Set timeout
    timer = threading.Timer(10, timeout_handler)
    timer.daemon = True
    timer.start()
    
    # Start webview
    webview.start()
    
    # Cancel timer if window closed successfully
    timer.cancel()
    print("✓ Window closed successfully")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 50)
print("Test complete")
print("=" * 50)

# Keep console open
input("\nPress Enter to exit...")