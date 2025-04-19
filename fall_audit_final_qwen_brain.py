import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import csv
import os
import subprocess
import threading
import re
import json
import textwrap
import sys
from typing import List, Tuple, Union, Optional

###############################################################################
# Config-related constants and helper functions
###############################################################################

CONFIG_FILE = "config.json"
AI_BRAIN_PATH = ""     # Will be set after we read or prompt the user
LLAMA_EXEC = ""        # Full path to llamafile-0.9.0.exe
MODEL_PATH = ""        # Full path to Qwen2.5-7B-Instruct-Q4_K_M.gguf

def load_config() -> dict:
    """
    Load configuration from CONFIG_FILE if it exists,
    otherwise return an empty dictionary.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg: dict) -> None:
    """
    Save the given dictionary to CONFIG_FILE in JSON format.
    """
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def get_ai_brain_path(master: tk.Tk) -> Optional[str]:
    """
    Check the config file for 'ai_brain_path'. If it's missing or invalid,
    prompt the user to pick the folder that contains llamafile-0.9.0.exe
    and Qwen2.5-7B-Instruct-Q4_K_M.gguf.

    Returns a valid folder path or None if the user cancels or picks an invalid folder.
    """
    config = load_config()
    path = config.get("ai_brain_path", "")

    # If path is set and actually exists, and we see the needed files, just return it
    if path:
        llama_exec = os.path.join(path, "llamafile-0.9.0.exe")
        model_file = os.path.join(path, "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
        if os.path.exists(llama_exec) and os.path.exists(model_file):
            return path

    # Otherwise, prompt the user to locate the AI Brain folder
    messagebox.showinfo(
        "AI Brain Folder Required",
        "Please locate the folder where AI Brain is installed.\n\n"
        "It should contain 'llamafile-0.9.0.exe' and 'Qwen2.5-7B-Instruct-Q4_K_M.gguf'."
    )
    new_path = filedialog.askdirectory(title="Select AI Brain Folder")
    if not new_path:
        # User cancelled
        return None

    # Validate the required files are there
    llama_exec = os.path.join(new_path, "llamafile-0.9.0.exe")
    model_file = os.path.join(new_path, "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    if not (os.path.exists(llama_exec) and os.path.exists(model_file)):
        messagebox.showerror(
            "Invalid Folder",
            "The required files were not found in that folder.\n"
            "Please make sure you select the correct AI Brain folder."
        )
        return None

    # Save the new path to config
    config["ai_brain_path"] = new_path
    save_config(config)
    return new_path


###############################################################################
# Core Logic
###############################################################################

class AIProcessingCore:
    """
    Handles logic for calling the LLM and storing results.
    """

    def __init__(self, output_path: str) -> None:
        self.output_path = output_path
        self.results: List[Tuple[str, Union[str, dict]]] = []
        self.rows_processed = 0
        self.total_rows = 0
        self.cancel_requested = False
        self.processing_done = False

    def build_prompt(self, prompt_text: str) -> str:
        """
        Build the LLM prompt from a single row's text.
        """
        whole_prompt = textwrap.dedent(f"""
            <|im_start|>system
            You are a helpful auditing assistant with extensive aged care nursing experience and care about elderly people<|im_end|>\n
            <|im_start|>user
            Please check this progress note for evidence of falls.
            Your response will be in JSON format with 'true' where there is evidence and 'false' where there is not evidence.
            Here is the note: {prompt_text}
            Example reply format: ```json{{"falls": "true/false"}}```<|im_end|>\n

            <|im_start|>assistant""")
        return whole_prompt

    def call_llm(self, prompt: str) -> str:
        """
        Call the LLM via subprocess and return the LLM's output (stdout),
        hiding the llamafile console on Windows.
        """
        # Only define creationflags on Windows
        CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

        # Use the global LLAMA_EXEC and MODEL_PATH
        result = subprocess.run(
            [LLAMA_EXEC, "-m", MODEL_PATH, "-p", prompt, "--temp", "0.03", "--log-disable"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=CREATE_NO_WINDOW  # <--- This hides the console on Windows
        )

        if result.returncode != 0:
            return f"Error: LLM execution failed. {result.stderr}"

        return result.stdout.strip() if result.stdout else ""

    def parse_llm_output(self, llm_output: str) -> Union[str, dict]:
        """
        Extract JSON from the LLM's response or return error strings if needed.
        """
        if not llm_output:
            return "Error: No response from model"

        # Attempt to find JSON in triple backticks
        matches = re.findall(r'```json\s*(\{.*?\})\s*```', llm_output, re.DOTALL)
        if matches:
            # Use the last match (in case there's an example or multiple outputs)
            json_text = matches[-1]
            try:
                return json.loads(json_text)
            except json.JSONDecodeError as e:
                return f"Error parsing JSON: {e}"
        else:
            return "No JSON found in response"

    def process_rows(self, rows: List[List[str]]) -> None:
        """
        Processes each CSV row, passing it to the LLM and storing the result.
        """
        self.total_rows = len(rows)
        self.rows_processed = 0

        for row in rows:
            if self.cancel_requested:
                break

            prompt_text = " ".join(row).strip()
            if not prompt_text:
                # If the row is empty, skip
                self.results.append(("", ""))
                self.rows_processed += 1
                continue

            # Build LLM prompt
            prompt_str = self.build_prompt(prompt_text)
            llm_output = self.call_llm(prompt_str)

            # If there's a known error from call_llm, store it
            if llm_output.startswith("Error: LLM execution failed."):
                self.results.append((prompt_text, llm_output))
                self.rows_processed += 1
                continue

            parsed_answer = self.parse_llm_output(llm_output)
            self.results.append((prompt_text, parsed_answer))
            self.rows_processed += 1

        self.processing_done = True

    def save_results(self) -> None:
        """
        Save the processed results to the output CSV file.
        Only the 'falls' value is written if the response is a dict with 'falls'.
        Otherwise, write the entire response as-is.
        """
        if not self.output_path:
            return
        
        with open(self.output_path, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.writer(outfile)
            # Write the header
            writer.writerow(["Progres note", "Falls Detected?"])
            
            for prompt, response in self.results:
                # If the model returned a dictionary with a 'falls' key, extract it
                if isinstance(response, dict) and "falls" in response:
                    final_answer = response["falls"]
                else:
                    # If we didn't get the expected JSON, just write the entire response
                    final_answer = response
                
                writer.writerow([prompt, final_answer])


###############################################################################
# GUI
###############################################################################

class AIProcessingGUI:
    """
    GUI class that handles all Tkinter interface and uses AIProcessingCore for logic.
    """

    def __init__(self, master: tk.Tk) -> None:
        self.master = master
        self.master.title("Fall Auditing Tool")

        # Variables to store file paths
        self.csv_path = tk.StringVar(value="")
        self.save_path = tk.StringVar(value="")

        # The logic class
        self.core: Optional[AIProcessingCore] = None
        # Storage for CSV rows
        self.rows: List[List[str]] = []

        self.build_main_window()

    def build_main_window(self) -> None:
        """
        Create the main window UI with file selectors and main buttons.
        """
        main_frame = ttk.Frame(self.master, padding=20)
        main_frame.pack(fill="both", expand=True)

        # Input CSV
        ttk.Label(main_frame, text="Select progress note file to audit (.CSV file only):").pack(anchor="w", pady=(0, 10))

        file_frame = ttk.Frame(main_frame)
        file_frame.pack(anchor="w", fill="x", pady=(0, 20))

        # Normal Entry (white background) for CSV path
        self.csv_entry = ttk.Entry(file_frame, textvariable=self.csv_path, width=50)
        self.csv_entry.pack(side="left", padx=(0, 10))
        ttk.Button(file_frame, text="Select File", command=self.select_file).pack(side="left")

        # Save path
        ttk.Label(main_frame, text="Select where to save the report:").pack(anchor="w", pady=(0, 10))

        save_frame = ttk.Frame(main_frame)
        save_frame.pack(anchor="w", fill="x", pady=(0, 20))

        # Normal Entry (white background) for Save path
        self.save_entry = ttk.Entry(save_frame, textvariable=self.save_path, width=50)
        self.save_entry.pack(side="left", padx=(0, 10))
        ttk.Button(save_frame, text="Save To", command=self.select_save_path).pack(side="left")

        # Buttons (Start, Reset, Close)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(anchor="center", pady=10)

        ttk.Button(btn_frame, text="Start Processing", command=self.start_processing).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="Reset", command=self.reset_fields).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="Close", command=self.close_app).pack(side="left", padx=10)

    def select_file(self) -> None:
        """
        Prompt the user to choose a CSV file for input.
        """
        path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
        if path:
            self.csv_path.set(path)

    def select_save_path(self) -> None:
        """
        Prompt the user to choose the output CSV path.
        """
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")])
        if path:
            self.save_path.set(path)

    def reset_fields(self) -> None:
        """
        Clear out fields so the user can do another audit.
        """
        self.csv_path.set("")
        self.save_path.set("")
        self.rows = []
        self.core = None
        messagebox.showinfo("Reset", "Fields have been cleared.")

    def close_app(self) -> None:
        """
        Close the entire application.
        """
        self.master.destroy()

    def start_processing(self) -> None:
        """
        Validates fields, checks if the AI Brain path is set (and files exist),
        reads CSV, creates the logic object, and opens progress window.
        """
        # Check for blank fields
        if not self.csv_path.get():
            messagebox.showerror("Error", "No input CSV file selected.")
            return
        if not self.save_path.get():
            messagebox.showerror("Error", "No save path selected.")
            return

        # Check that both input file and save file end with .csv
        if not self.csv_path.get().lower().endswith(".csv"):
            messagebox.showerror("Error", "Input file must be a .csv file.")
            return
        if not self.save_path.get().lower().endswith(".csv"):
            messagebox.showerror("Error", "Save path must be a .csv file.")
            return

        # Check file existence
        if not os.path.exists(self.csv_path.get()):
            messagebox.showerror("Error", "Input file does not exist.")
            return

        # Read CSV file
        try:
            with open(self.csv_path.get(), "r", newline="", encoding="utf-8") as infile:
                reader = csv.reader(infile)
                self.rows = list(reader)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read the CSV file.\n{e}")
            return

        # Create the logic handler
        self.core = AIProcessingCore(output_path=self.save_path.get())
        self.open_progress_window()

    def open_progress_window(self) -> None:
        """
        Opens a progress window while the LLM processing is ongoing.
        The progress window is locked on top of the main window and prevents interaction with it.
        Also centers itself over the main window, and is not resizable.
        """
        self.progress_win = tk.Toplevel(self.master)
        self.progress_win.title("Processing Progress")

        # Disable window resizing
        self.progress_win.resizable(False, False)

        # Make the progress window modal and always on top
        self.progress_win.transient(self.master)
        self.progress_win.grab_set()
        self.progress_win.focus_set()
        self.progress_win.attributes('-topmost', True)

        frame = ttk.Frame(self.progress_win, padding=20)
        frame.pack(fill="both", expand=True)

        self.progress_label = ttk.Label(frame, text="Starting...")
        self.progress_label.pack(anchor="w", pady=(0, 10))

        self.progress_bar = ttk.Progressbar(frame, orient="horizontal", length=300, mode="determinate")
        self.progress_bar.pack(pady=(0, 10))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(10, 0))

        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self.request_cancel)
        cancel_btn.pack(side="left", padx=(10, 0))

        self.open_file_btn = ttk.Button(btn_frame, text="Open Report", state="disabled", command=self.open_output_file)
        self.open_file_btn.pack(side="left", padx=(10, 0))

        # Set progress bar's max to total rows
        self.progress_bar["maximum"] = len(self.rows)

        # --- Center the new window over the main window ---
        self.progress_win.update_idletasks()
        main_x = self.master.winfo_x()
        main_y = self.master.winfo_y()
        main_w = self.master.winfo_width()
        main_h = self.master.winfo_height()
        win_w = self.progress_win.winfo_width()
        win_h = self.progress_win.winfo_height()
        x = main_x + (main_w // 2) - (win_w // 2)
        y = main_y + (main_h // 2) - (win_h // 2)
        self.progress_win.geometry(f"+{x}+{y}")
        # --------------------------------------------------

        # Run in background thread
        thread = threading.Thread(target=self.run_core_logic, daemon=True)
        thread.start()

        # Update progress every 500ms
        self.update_progress()

    def request_cancel(self) -> None:
        """
        Set a flag to cancel the processing loop.
        """
        if self.core and messagebox.askyesno("Cancel", "Are you sure you want to cancel?"):
            self.core.cancel_requested = True

    def run_core_logic(self) -> None:
        """
        Invoke core logic to process rows and then save results.
        """
        if not self.core:
            return
        self.core.process_rows(self.rows)
        self.core.save_results()

    def update_progress(self) -> None:
        """
        Poll for row processing progress and update the bar/label.
        """
        if not self.core:
            self.progress_label.config(text="No core logic yet.")
            self.progress_win.after(500, self.update_progress)
            return

        self.progress_bar["value"] = self.core.rows_processed
        total_rows = len(self.rows)

        if total_rows > 0:
            status_text = f"Processed {self.core.rows_processed}/{total_rows} lines"
            if self.core.cancel_requested:
                status_text += " (Cancel requested...)"
            self.progress_label.config(text=status_text)
        else:
            self.progress_label.config(text="Reading file...")

        if self.core.processing_done and (
            self.core.rows_processed == total_rows or self.core.cancel_requested
        ):
            # Done
            self.open_file_btn.config(state="normal")
            self.progress_label.config(text="Done. The AI has reviewed the progress notes.")
        else:
            # Continue polling
            self.progress_win.after(500, self.update_progress)

    def open_output_file(self) -> None:
        """
        Open the final CSV in default viewer and close the progress window.
        """
        if not self.core:
            return
        if os.path.exists(self.core.output_path):
            os.startfile(self.core.output_path)  # Windows-specific
            self.progress_win.destroy()
        else:
            messagebox.showerror("Error", "Output file not found.")


###############################################################################
# Main Entry Point
###############################################################################

def main() -> None:
    # Create a minimal root to prompt for AI Brain path if needed
    root = tk.Tk()
    root.withdraw()  # Hide the main window until we confirm AI Brain path

    # Attempt to get the AI Brain path from config or user selection
    global AI_BRAIN_PATH, LLAMA_EXEC, MODEL_PATH
    path = get_ai_brain_path(root)
    if not path:
        # User canceled or invalid folder chosen
        messagebox.showerror("Error", "Cannot locate the AI Brain folder. Exiting.")
        sys.exit(1)

    # If valid, set our global variables
    AI_BRAIN_PATH = path
    LLAMA_EXEC = os.path.join(AI_BRAIN_PATH, "llamafile-0.9.0.exe")
    MODEL_PATH = os.path.join(AI_BRAIN_PATH, "Qwen2.5-7B-Instruct-Q4_K_M.gguf")

    # Now show the main GUI
    root.deiconify()
    AIProcessingGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
