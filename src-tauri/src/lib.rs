use tauri::Manager;
use tauri_plugin_shell::ShellExt;

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Spawn the Python backend as a sidecar process
            let shell = app.shell();
            let sidecar = shell
                .sidecar("binaries/quickdocker-backend")
                .expect("failed to create sidecar command");

            let (mut _rx, child) = sidecar.spawn().expect("failed to spawn backend sidecar");

            // Store the child PID so we can clean up on exit
            app.manage(BackendProcess(std::sync::Mutex::new(Some(child))));

            // Wait a moment for the backend to start, then navigate
            println!("QuickDocker: backend sidecar started");

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Kill the backend when the window is closed
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                            println!("QuickDocker: backend sidecar stopped");
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

struct BackendProcess(std::sync::Mutex<Option<tauri_plugin_shell::process::CommandChild>>);
