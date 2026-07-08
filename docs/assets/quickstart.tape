Output docs/assets/quickstart.gif
Set FontSize 15
Set Width 1000
Set Height 550
Set TypingSpeed 40ms
Type "python"
Enter
Sleep 1s
Type "from lean_memory import Memory"
Enter
Type "mem = Memory(root='./demo-data')"
Enter
Sleep 500ms
Type "mem.add('me', 'I work at Acme Corp.')"
Enter
Sleep 1s
Type "mem.add('me', 'I now work at Globex.')  # supersedes Acme"
Enter
Sleep 1s
Type "mem.search('me', 'where does the user work?')[0].fact.fact_text"
Enter
Sleep 2s
Type "exit()"
Enter
Sleep 1s
