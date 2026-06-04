@echo off
:: hive-mind Windows host setup (run as Administrator once per machine)
:: Idempotent - safe to run multiple times
:: Does: OpenSSH install, DefaultShell -> bash.exe, portproxy for port 9876, firewall rule

echo === hive-mind Windows host setup ===
echo.

:: ---- 1. Install OpenSSH Server if missing ----
sc query sshd >nul 2>&1
if errorlevel 1 (
    echo Installing OpenSSH Server...
    powershell -NoProfile -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
) else (
    echo [OK] OpenSSH Server already installed
)

:: ---- 2. Set DefaultShell to a WSL wrapper that accepts -c ----
:: OpenSSH invokes the shell as `shell -c "<cmd>"`. wsl.exe rejects -c, and the
:: per-user bash.exe is a 0-byte stub on some installs (e.g. gregorius). A tiny
:: wrapper calling `wsl.exe bash %*` works on EVERY node — no hardcoded username,
:: no stub problem. (corpus facts #15, #21)
echo Writing C:\ssh-wsl.bat wrapper...
> C:\ssh-wsl.bat echo @echo off
>> C:\ssh-wsl.bat echo C:\Windows\System32\wsl.exe bash %%*
echo Setting OpenSSH DefaultShell to C:\ssh-wsl.bat...
reg add "HKEY_LOCAL_MACHINE\SOFTWARE\OpenSSH" /v DefaultShell /t REG_SZ /d "C:\ssh-wsl.bat" /f
echo [OK] DefaultShell set

:: ---- 3. Enable and start sshd ----
sc config sshd start=auto >nul
net stop sshd >nul 2>&1
net start sshd
echo [OK] sshd running

:: ---- 4. Get WSL IP and set portproxy for port 9876 ----
echo Getting WSL IP...
for /f "tokens=*" %%i in ('wsl.exe -e bash -c "ip addr show eth0 | grep -oP \"(?<=inet )[\d.]+\""') do set WSL_IP=%%i
echo WSL IP: %WSL_IP%

netsh interface portproxy delete v4tov4 listenport=9876 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add v4tov4 listenport=9876 listenaddress=0.0.0.0 connectport=9876 connectaddress=%WSL_IP%
echo [OK] portproxy 0.0.0.0:9876 -> %WSL_IP%:9876

:: ---- 5. Firewall rules ----
netsh advfirewall firewall delete rule name="HiveMind Sync 9876" >nul 2>&1
netsh advfirewall firewall add rule name="HiveMind Sync 9876" dir=in action=allow protocol=TCP localport=9876
echo [OK] Firewall rule for port 9876

:: ---- 6. Show current portproxy state ----
echo.
echo Current portproxy rules:
netsh interface portproxy show v4tov4

:: ---- 7. Auto-start WSL at logon (Windows-reboot persistence) ----
:: The systemd hive-sync.service (installed by 03-daemon-service.sh) starts the
:: daemon when WSL boots, but WSL only boots on demand. This logon task boots the
:: default distro (with systemd=true it then stays up and starts the enabled
:: service automatically). Verify on the box: after a reboot+login, from WSL run
:: `systemctl is-active hive-sync.service`. Remove with:
::   schtasks /delete /tn "HiveMind WSL Autostart" /f
echo Creating WSL autostart logon task...
schtasks /create /tn "HiveMind WSL Autostart" /tr "C:\Windows\System32\wsl.exe -e true" /sc onlogon /f
echo [OK] WSL autostart task created

echo.
echo === Windows host setup complete ===
echo Next: run scripts/install/02-wsl-setup.sh from inside WSL
pause
