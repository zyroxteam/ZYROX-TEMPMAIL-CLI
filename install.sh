#!/data/data/com.termux/files/usr/bin/bash
# ZYROX TempMail CLI — Termux installer
set -e
echo "🔧 ZYROX TempMail CLI — setup ho raha hai..."
pkg install -y python >/dev/null 2>&1 || true
pip install --quiet requests || pip3 install --quiet requests
chmod +x tmn.py
echo ""
echo "✅ SETUP DONE 😈"
echo "Run karo:  python tmn.py"
echo "Auto mode: python tmn.py auto"
