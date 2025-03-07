import os
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import imagehash

class UnionFind:
    """Classe simples de Union-Find (Disjoint Set)."""
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0]*n

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        rootX = self.find(x)
        rootY = self.find(y)
        if rootX != rootY:
            if self.rank[rootX] < self.rank[rootY]:
                self.parent[rootX] = rootY
            elif self.rank[rootX] > self.rank[rootY]:
                self.parent[rootY] = rootX
            else:
                self.parent[rootY] = rootX
                self.rank[rootX] += 1

class ImageCleaner:
    def __init__(self, master):
        self.master = master
        self.master.title("Image Cleaner")
        self.groups = []
        self.images_data = []
        self.selected_folder = ""
        self.create_widgets()

    def create_widgets(self):
        self.select_btn = tk.Button(self.master, text="Selecionar Pasta", command=self.select_folder)
        self.select_btn.pack(pady=10)

    def select_folder(self):
        folder = filedialog.askdirectory(title="Selecione a pasta com imagens")
        if folder:
            self.selected_folder = folder
            self.scan_folder()

    def scan_folder(self):
        self.images_data = []
        valid_extensions = [".jpg", ".jpeg", ".png", ".bmp", ".gif"]
        for root, dirs, files in os.walk(self.selected_folder):
            for file in files:
                if os.path.splitext(file)[1].lower() in valid_extensions:
                    filepath = os.path.join(root, file)
                    try:
                        with Image.open(filepath) as img:
                            # Teste com phash, por exemplo
                            hash_val = imagehash.phash(img)
                        self.images_data.append((filepath, hash_val))
                    except Exception as e:
                        print(f"Erro ao processar {filepath}: {e}")

        # Ajuste o threshold conforme necessário
        self.group_images(threshold=10)

    def group_images(self, threshold=10):
        """
        Em vez de agrupar sequencialmente, criaremos um grafo de similaridade,
        onde cada imagem é um nó e há uma aresta entre duas imagens se diff <= threshold.
        Depois, usamos Union-Find para achar as componentes conexas.
        """
        n = len(self.images_data)
        if n == 0:
            messagebox.showinfo("Resultado", "Nenhuma imagem encontrada.")
            return

        uf = UnionFind(n)

        # Compara todos os pares de imagens (O(n^2))
        for i in range(n):
            for j in range(i+1, n):
                diff = abs(self.images_data[i][1] - self.images_data[j][1])
                if diff <= threshold:
                    # União dos conjuntos
                    uf.union(i, j)

        # Depois de unificar, cada imagem terá um "representante" (root)
        # Agrupamos as imagens de acordo com esse root
        root_to_group = {}
        for i in range(n):
            root_i = uf.find(i)
            if root_i not in root_to_group:
                root_to_group[root_i] = []
            root_to_group[root_i].append(self.images_data[i])

        # Filtra grupos que tenham mais de 1 imagem
        self.groups = [group for group in root_to_group.values() if len(group) > 1]

        if not self.groups:
            messagebox.showinfo("Resultado", "Nenhuma imagem similar encontrada.")
        else:
            self.show_groups()

    def show_groups(self):
        """Cria uma janela listando os grupos de imagens semelhantes com opções de mover ou excluir."""
        self.groups_window = tk.Toplevel(self.master)
        self.groups_window.title("Grupos de Imagens Similares")

        for idx, group in enumerate(self.groups):
            frame = tk.LabelFrame(self.groups_window, text=f"Grupo {idx + 1}", padx=10, pady=10)
            frame.pack(padx=10, pady=10, fill="x")

            check_vars = []

            for filepath, _ in group:
                var = tk.IntVar()
                check_vars.append(var)
                try:
                    img = Image.open(filepath)
                    img.thumbnail((100, 100))
                    photo = ImageTk.PhotoImage(img)
                    lbl = tk.Label(frame, image=photo)
                    lbl.image = photo
                    lbl.pack(side="left", padx=5, pady=5)
                    chk = tk.Checkbutton(frame, text=filepath, variable=var)
                    chk.pack(side="left", padx=5, pady=5)
                except Exception as e:
                    print(f"Erro ao carregar imagem {filepath}: {e}")

            btn_move = tk.Button(frame, text="Mover Selecionadas",
                                 command=lambda grp=group, vars=check_vars: self.move_images(grp, vars))
            btn_move.pack(side="left", padx=5, pady=5)
            btn_delete = tk.Button(frame, text="Excluir Selecionadas",
                                   command=lambda grp=group, vars=check_vars: self.delete_images(grp, vars))
            btn_delete.pack(side="left", padx=5, pady=5)

    def move_images(self, group, check_vars):
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return
        for (filepath, _), var in zip(group, check_vars):
            if var.get() == 1:
                try:
                    basename = os.path.basename(filepath)
                    new_path = os.path.join(dest_folder, basename)
                    os.rename(filepath, new_path)
                except Exception as e:
                    print(f"Erro ao mover {filepath}: {e}")
        messagebox.showinfo("Mover", "Operação de mover concluída!")

    def delete_images(self, group, check_vars):
        confirm = messagebox.askyesno("Excluir", "Tem certeza que deseja excluir as imagens selecionadas?")
        if not confirm:
            return
        for (filepath, _), var in zip(group, check_vars):
            if var.get() == 1:
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"Erro ao excluir {filepath}: {e}")
        messagebox.showinfo("Excluir", "Operação de exclusão concluída!")

if __name__ == "__main__":
    root = tk.Tk()
    app = ImageCleaner(root)
    root.mainloop()
