import sys
import webview
import time

print("=" * 60)
print("MINIMAL PYWEBVIEW TEST")
print("=" * 60)

print(f"Python: {sys.version}")
print(f"Platform: {sys.platform}")
print(f"Webview: {webview.__version__ if hasattr(webview, '__version__') else 'unknown'}")

# Create a very simple window
html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Test</title>
</head>
<body style="background: #2ecc71; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; font-family: Arial;">
    <div style="text-align: center; color: white;">
        <h1>✅ Test Window</h1>
        <p>If you can see this, pywebview is working!</p>
        <p style="font-size: 12px; margin-top: 50px;">Close this window to continue test</p>
    </div>
</body>
</html>
"""

print("\n1️⃣ Creating window...")
try:
    window = webview.create_window(
        'PyWebView Test',
        html=html_content,
        width=500,
        height=400,
        resizable=True
    )
    print("✅ Window created successfully")
except Exception as e:
    print(f"❌ Error creating window: {e}")
    sys.exit(1)

print("\n2️⃣ Starting webview (window should appear)...")
print("   If nothing happens in 10 seconds, there might be an issue")

# Try different backends if available
backends = ['cef', 'gtk', 'qt', 'edgechromium', 'mshtml']  # Common backends
backend_success = False

for backend in backends:
    print(f"\n3️⃣ Trying backend: {backend}")
    try:
        # Start with current backend
        webview.start(backend=backend, debug=True)
        backend_success = True
        break
    except Exception as e:
        print(f"   ❌ Backend {backend} failed: {e}")
        continue

if not backend_success:
    print("\n4️⃣ Trying default backend...")
    try:
        webview.start(debug=True)
        backend_success = True
    except Exception as e:
        print(f"   ❌ Default backend failed: {e}")

if backend_success:
    print("\n✅ Window was closed successfully")
else:
    print("\n❌ Failed to create window with any backend")

print("\n" + "=" * 60)
input("Press Enter to exit...")