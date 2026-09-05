# Registers the paper bot to run every 3 minutes as SYSTEM --
# runs whether you are logged in or not, no password stored.
# RUN THIS ONCE, AS ADMINISTRATOR:
#   right-click Start > Terminal (Admin), then:
#   powershell -ExecutionPolicy Bypass -File C:\Users\mckin\polymarket-paperbot\setup_task.ps1

$py  = "C:\Python311\python.exe"
$dir = "C:\Users\mckin\polymarket-paperbot"

$action    = New-ScheduledTaskAction -Execute $py -Argument "$dir\paperbot.py once" -WorkingDirectory $dir
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
                -RepetitionInterval (New-TimeSpan -Minutes 3) `
                -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "PolymarketPaperBot" `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force

Write-Host "`nDone. Verifying:" -ForegroundColor Green
Get-ScheduledTask -TaskName "PolymarketPaperBot" | Select-Object TaskName, State
Start-ScheduledTask -TaskName "PolymarketPaperBot"
Start-Sleep 12
Get-Content "$dir\bot.log" -Tail 4
