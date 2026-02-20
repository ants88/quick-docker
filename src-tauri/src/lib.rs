use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            eprintln!("[QuickDocker] Starting up...");

            // Spawn the Python backend as a sidecar process
            let shell = app.shell();
            let sidecar = shell
                .sidecar("quickdocker-backend")
                .expect("failed to create sidecar command");

            eprintln!("[QuickDocker] Spawning backend sidecar...");

            let (mut rx, child) = sidecar.spawn().expect("failed to spawn backend sidecar");

            eprintln!("[QuickDocker] Backend sidecar spawned (pid: {})", child.pid());

            // Log sidecar stdout/stderr in a background thread
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            eprintln!("[backend:stdout] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("[backend:stderr] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(payload) => {
                            eprintln!("[QuickDocker] Backend terminated: code={:?}, signal={:?}",
                                payload.code, payload.signal);
                            break;
                        }
                        CommandEvent::Error(err) => {
                            eprintln!("[QuickDocker] Backend error: {}", err);
                        }
                        _ => {}
                    }
                }
            });

            // Store the child so we can clean up on exit
            app.manage(BackendProcess(std::sync::Mutex::new(Some(child))));

            eprintln!("[QuickDocker] Setup complete, window should open shortly");

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            eprintln!("[QuickDocker] Killing backend (pid: {})...", child.pid());
                            let _ = child.kill();
                            eprintln!("[QuickDocker] Backend stopped");
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

struct BackendProcess(std::sync::Mutex<Option<tauri_plugin_shell::process::CommandChild>>);
