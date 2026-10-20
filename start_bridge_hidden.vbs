Set WshShell = CreateObject("WScript.Shell")
' Run the batch file completely hidden (0 = hide window, false = do not wait)
WshShell.Run "cmd /c """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\start_bridge.bat""", 0, False
