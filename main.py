import os
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import imagehash

class ImageCleaner:
    def __init__(self, master):
        self.master = master
        self.master.title("Image Cleaner")
        self.groups = []        # Lista de grupos de imagens semelhantes
        self.images_data = []   # Lista de tuplas (caminho, hash)
        self.selected_folder = ""
        self.create_widgets()

    def create_widgets(self):
        # Botão para selecionar a pasta
        self.select_btn = tk.Button(self.master, text="Selecionar Pasta", command=self.select_folder)
        self.select_btn.pack(pady=10)

    def select_folder(self):
        folder = filedialog.askdirectory(title="Selecione a pasta com imagens")
        if folder:
            self.selected_folder = folder
            self.scan_folder()

    def scan_folder(self):
        """Percorre a pasta e suas subpastas, lendo imagens e calculando o hash."""
        self.images_data = []
        valid_extensions = [".jpg", ".jpeg", ".png", ".bmp", ".gif"]
        for root, dirs, files in os.walk(self.selected_folder):
            for file in files:
                if os.path.splitext(file)[1].lower() in valid_extensions:
                    filepath = os.path.join(root, file)
                    try:
                        with Image.open(filepath) as img:
                            # Calcula o hash perceptual (método average_hash)
                            hash_val = imagehash.average_hash(img)
                        self.images_data.append((filepath, hash_val))
                    except Exception as e:
                        print(f"Erro ao processar {filepath}: {e}")
        self.group_images()

    def group_images(self, threshold=5):
        """
        Agrupa imagens similares comparando a diferença dos hashes.
        Se a diferença for menor ou igual ao threshold, as imagens são consideradas similares.
        """
        self.groups = []
        used = [False] * len(self.images_data)
        for i in range(len(self.images_data)):
            if used[i]:
                continue
            group = [self.images_data[i]]
            used[i] = True
            for j in range(i + 1, len(self.images_data)):
                if not used[j]:
                    diff = abs(self.images_data[i][1] - self.images_data[j][1])
                    if diff <= threshold:
                        group.append(self.images_data[j])
                        used[j] = True
            if len(group) > 1:
                self.groups.append(group)

        if not self.groups:
            messagebox.showinfo("Resultado", "Nenhuma imagem similar encontrada.")
        else:
            self.show_groups()

    def show_groups(self):
        """Cria uma janela listando os grupos de imagens semelhantes com opções de mover ou excluir."""
        self.groups_window = tk.Toplevel(self.master)
        self.groups_window.title("Grupos de Imagens Similares")

        # Para cada grupo, cria um frame com as imagens (thumbnail + checkbox)
        for idx, group in enumerate(self.groups):
            frame = tk.LabelFrame(self.groups_window, text=f"Grupo {idx + 1}", padx=10, pady=10)
            frame.pack(padx=10, pady=10, fill="x")

            # Lista para armazenar as variáveis dos checkbuttons deste grupo
            check_vars = []

            # Cria uma miniatura e um checkbox para cada imagem do grupo
            for filepath, _ in group:
                var = tk.IntVar()
                check_vars.append(var)
                try:
                    img = Image.open(filepath)
                    img.thumbnail((100, 100))
                    photo = ImageTk.PhotoImage(img)
                    # Label para exibir a imagem
                    lbl = tk.Label(frame, image=photo)
                    lbl.image = photo  # mantém referência para não coletar lixo
                    lbl.pack(side="left", padx=5, pady=5)
                    # Checkbox com o nome do arquivo
                    chk = tk.Checkbutton(frame, text=os.path.basename(filepath), variable=var)
                    chk.pack(side="left", padx=5, pady=5)
                except Exception as e:
                    print(f"Erro ao carregar imagem {filepath}: {e}")

            # Botões de ação para o grupo (mover ou excluir as imagens selecionadas)
            btn_move = tk.Button(frame, text="Mover Selecionadas",
                                 command=lambda grp=group, vars=check_vars: self.move_images(grp, vars))
            btn_move.pack(side="left", padx=5, pady=5)
            btn_delete = tk.Button(frame, text="Excluir Selecionadas",
                                   command=lambda grp=group, vars=check_vars: self.delete_images(grp, vars))
            btn_delete.pack(side="left", padx=5, pady=5)

    def move_images(self, group, check_vars):
        """Move as imagens selecionadas para uma pasta escolhida pelo usuário."""
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return
        for (filepath, _), var in zip(group, check_vars):
            if var.get() == 1:
                try:
                    basename = os.path.basename(filepath)
                    new_path = os.path.join(dest_folder, basename)
                    os.rename(filepath, new_path)
                    print(f"Movido: {filepath} -> {new_path}")
                except Exception as e:
                    print(f"Erro ao mover {filepath}: {e}")
        messagebox.showinfo("Mover", "Operação de mover concluída!")

    def delete_images(self, group, check_vars):
        """Exclui as imagens selecionadas, após confirmação do usuário."""
        confirm = messagebox.askyesno("Excluir", "Tem certeza que deseja excluir as imagens selecionadas?")
        if not confirm:
            return
        for (filepath, _), var in zip(group, check_vars):
            if var.get() == 1:
                try:
                    os.remove(filepath)
                    print(f"Excluído: {filepath}")
                except Exception as e:
                    print(f"Erro ao excluir {filepath}: {e}")
        messagebox.showinfo("Excluir", "Operação de exclusão concluída!")

if __name__ == "__main__":
    root = tk.Tk()
    app = ImageCleaner(root)
    root.mainloop()
