# VHS script (github.com/charmbracelet/vhs) — renders the README hero GIF.
#   vhs examples/demo.tape   →  demo.gif
Output examples/demo.gif
Set FontSize 12
Set Width 900
Set Height 620
Set Theme "Catppuccin Mocha"
Type "cat examples/demo-corpus/deploy-flag.md"
Enter
Sleep 2s
Type "cat examples/demo-corpus/deploy-flag-update.md"
Enter
Sleep 2.5s
Type "lbrain query 'what flag do deploys use?'"
Enter
Sleep 6s
