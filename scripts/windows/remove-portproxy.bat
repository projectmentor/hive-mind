@echo off
:: ============================================================
:: hive-mind: remove stale portproxy rules (run as Administrator)
::
:: Only needed on machines that previously used the Win OpenSSH
:: + portproxy setup. Safe to run multiple times.
:: ============================================================
echo Removing stale HiveMind portproxy rules...
netsh interface portproxy delete v4tov4 listenport=9876 listenaddress=0.0.0.0 >nul 2>&1
netsh advfirewall firewall delete rule name="HiveMind Sync 9876" >nul 2>&1
echo Done. Current portproxy rules (should be empty):
netsh interface portproxy show all
pause
