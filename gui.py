import sys
import os
from tkinter import Tk, Label, StringVar

IS_RPI = sys.platform == "linux" and os.path.exists("/proc/device-tree/model")


class OrdeneGUI:
    def __init__(self):
        self.root = Tk()
        self.root.title("Ordeñe en Curso")
        self.root.configure(bg="#f2f2f2")

        if IS_RPI:
            self.root.attributes('-fullscreen', True)
        else:
            self.root.geometry("800x600")

        self.root.option_add('*Font', 'Arial 20')

        self.rodeo_var = StringVar(value="Ninguno")
        self.litros_rodeo_var = StringVar(value="0.00 L")
        self.litros_total_var = StringVar(value="0.00 L")
        self.caudal_var = StringVar(value="--- L/min")

        Label(self.root, text="Rodeo actual", font=("Arial", 28, "bold"), bg="#f2f2f2").pack(pady=(30, 0))
        Label(self.root, textvariable=self.rodeo_var, font=("Arial", 64, "bold"), fg="#1e90ff", bg="#f2f2f2").pack(pady=(5, 20))

        Label(self.root, text="Caudal", font=("Arial", 22, "bold"), bg="#f2f2f2").pack()
        Label(self.root, textvariable=self.caudal_var, font=("Arial", 36, "bold"), fg="#9932cc", bg="#f2f2f2").pack(pady=5)

        Label(self.root, text="Litros del rodeo", font=("Arial", 26, "bold"), bg="#f2f2f2").pack()
        Label(self.root, textvariable=self.litros_rodeo_var, font=("Arial", 54, "bold"), fg="#008000", bg="#f2f2f2").pack(pady=5)

        Label(self.root, text="Total del ordeñe", font=("Arial", 26, "bold"), bg="#f2f2f2").pack()
        Label(self.root, textvariable=self.litros_total_var, font=("Arial", 42, "bold"), fg="#ff4500", bg="#f2f2f2").pack(pady=5)

        if not IS_RPI:
            self.help_var = StringVar(value="[PC] Teclas 1-7 = rodeos | Esc = salir")
            Label(self.root, textvariable=self.help_var, font=("Arial", 12),
                  fg="#888888", bg="#f2f2f2").pack(side="bottom", pady=10)
            self.root.bind("<Escape>", lambda e: self.root.destroy())

    def update_display(self, rodeo, litros_rodeo, litros_total, caudal=None):
        self.rodeo_var.set(rodeo if rodeo else "Ninguno")
        self.litros_rodeo_var.set(f"{litros_rodeo:.2f} L")
        self.litros_total_var.set(f"{litros_total:.2f} L")
        if caudal is not None:
            self.caudal_var.set(f"{caudal:.1f} L/min")
        else:
            self.caudal_var.set("--- L/min")

    def loop(self):
        self.root.mainloop()

def show_shutdown_screen():
    """Muestra una pantalla completa con el mensaje 'Apagando...'"""
    try:
        root = Tk()
        root.attributes('-fullscreen', True)
        root.configure(bg='black')

        label = Label(
            root,
            text="APAGANDO...",
            font=("Arial", 60, "bold"),
            fg="white",
            bg="black"
        )
        label.pack(expand=True)

        root.update()
        return root
    except Exception as e:
        print(f"[GUI] Error mostrando pantalla de apagado: {e}")
        return None
