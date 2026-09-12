"""PyInstaller's entry point. Kept separate from grandice.desktop so the
package itself has no PyInstaller-specific code beyond skills.py's
sys.frozen check — this file exists only because PyInstaller analyzes a
script, not a console-script entry point."""

from grandice.desktop import main

if __name__ == "__main__":
    main()
