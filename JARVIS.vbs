Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw  = here & "\.venv\Scripts\pythonw.exe"
app  = here & "\app.py"
If Not fso.FileExists(pyw) Then
  MsgBox "Virtual environment missing." & vbCrLf & vbCrLf & _
         "Run this once in the project folder:" & vbCrLf & _
         "  python -m venv .venv" & vbCrLf & _
         "  .venv\Scripts\pip install -r requirements.txt", _
         vbCritical, "JARVIS"
  WScript.Quit 1
End If
CreateObject("WScript.Shell").Run """" & pyw & """ """ & app & """", 0, False
