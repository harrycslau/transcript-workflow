-- Brain launcher applet source (human-readable reference).
--
-- This AppleScript is compiled with macOS's built-in
-- `/usr/bin/osacompile -o Brain.app Brain-launch.applescript` so the
-- bundle's CFBundleExecutable is a native Mach-O applet instead of a text
-- shell script. A native entry point is important on Apple Silicon:
-- Launch Services must never mistake the bundle for an Intel-only or
-- script executable and ask for Rosetta.
--
-- The applet only locates its own bundle and runs the bundled
-- `Contents/Resources/Brain-launch.zsh`, which owns the real launch
-- behavior. A handled nonzero shell exit (the launcher already showed its
-- own sanitized guidance dialog) is swallowed so macOS does not add a
-- second, generic error dialog.

on run
	try
		set appPath to POSIX path of (path to me)
		set launcherPath to appPath & "Contents/Resources/Brain-launch.zsh"
		with timeout of 300 seconds
			do shell script "/bin/zsh " & quoted form of launcherPath
		end timeout
	on error
		-- Handled launcher failure: the zsh resource reports it.
	end try
end run
