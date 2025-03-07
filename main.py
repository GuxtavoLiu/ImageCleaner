import os
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import imagehash
from datetime import datetime

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

def get_file_md5(filepath):
    """
    Retorna o hash MD5 de um arquivo.
    Se dois arquivos tiverem o mesmo MD5, são bit-a-bit idênticos.
    """
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

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
                        # Calcula perceptual hash
                        with Image.open(filepath) as img:
                            hash_val = imagehash.phash(img)
                        # Calcula MD5 para detectar arquivos idênticos
                        md5_val = get_file_md5(filepath)
                        # Armazena tupla com (caminho, p-hash, md5)
                        self.images_data.append((filepath, hash_val, md5_val))
                    except Exception as e:
                        print(f"Erro ao processar {filepath}: {e}")

        # Ajuste o threshold conforme necessário
        self.group_images(threshold=10)

    def group_images(self, threshold=10):
        """
        Cria um grafo de similaridade usando o Union-Find.
        Cada imagem é um nó e há uma aresta se diff <= threshold.
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
                    uf.union(i, j)

        # Agrupa de acordo com o root
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
        """Exibe a janela listando os grupos de imagens com opções de mover ou excluir,
           dentro de um canvas com scrollbar."""
        self.groups_window = tk.Toplevel(self.master)
        self.groups_window.title("Grupos de Imagens Similares")

        # --- 1) Cria um Frame para conter o Canvas e a Scrollbar ---
        scroll_container = tk.Frame(self.groups_window)
        scroll_container.pack(fill="both", expand=True)

        # --- 2) Cria o Canvas ---
        canvas = tk.Canvas(scroll_container)
        canvas.pack(side="left", fill="both", expand=True)

        # --- 3) Cria a Scrollbar e vincula ao Canvas ---
        scrollbar = tk.Scrollbar(scroll_container, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        # --- 4) Cria o Frame que conterá todo o conteúdo (grupos, imagens, etc.) ---
        content_frame = tk.Frame(canvas)
        # Insere o content_frame dentro do canvas como uma "janela"
        canvas.create_window((0, 0), window=content_frame, anchor="nw")

        # Função para ajustar a região de rolagem sempre que o content_frame mudar de tamanho
        def on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        content_frame.bind("<Configure>", on_configure)

        # --- A partir daqui, criamos os grupos dentro de content_frame ---
        for idx, group in enumerate(self.groups):
            frame = tk.LabelFrame(content_frame, text=f"Grupo {idx + 1}", padx=10, pady=10)
            frame.pack(padx=10, pady=10, fill="x", expand=True)

            check_vars = []

            # Primeiro, vamos contar quantas vezes cada MD5 aparece no grupo
            md5_count = {}
            for (_, _, md5_val) in group:
                md5_count[md5_val] = md5_count.get(md5_val, 0) + 1

            # Exibe cada imagem do grupo
            for filepath, p_hash, md5_val in group:
                var = tk.IntVar()
                check_vars.append(var)

                # Monta um frame interno para cada imagem
                item_frame = tk.Frame(frame)
                item_frame.pack(side="top", fill="x", pady=5)

                # Miniatura
                try:
                    img = Image.open(filepath)
                    img.thumbnail((100, 100))
                    photo = ImageTk.PhotoImage(img)
                    lbl_img = tk.Label(item_frame, image=photo)
                    lbl_img.image = photo
                    lbl_img.pack(side="left", padx=5)
                except Exception as e:
                    print(f"Erro ao carregar imagem {filepath}: {e}")
                    lbl_img = tk.Label(item_frame, text="(Erro ao carregar)")
                    lbl_img.pack(side="left", padx=5)

                # Área de texto e checkbox
                text_frame = tk.Frame(item_frame)
                text_frame.pack(side="left", fill="both", expand=True)

                # Checkbutton
                chk = tk.Checkbutton(text_frame, text="Selecionar", variable=var)
                chk.pack(anchor="w")

                # Verifica se a imagem é idêntica (MD5 duplicado) ou apenas semelhante
                if md5_count[md5_val] > 1:
                    status = "Idêntica"
                else:
                    status = "Semelhante"

                # Metadados do arquivo
                size_bytes = os.path.getsize(filepath)
                ctime = os.path.getctime(filepath)
                mtime = os.path.getmtime(filepath)

                from datetime import datetime
                ctime_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M:%S")
                mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

                info_text = (
                    f"Caminho: {filepath}\n"
                    f"Status: {status}\n"
                    f"Tamanho: {size_bytes} bytes\n"
                    f"Criado em: {ctime_str}\n"
                    f"Modificado em: {mtime_str}\n"
                )

                lbl_info = tk.Label(text_frame, text=info_text, justify="left", anchor="w")
                lbl_info.pack(anchor="w")

            # Botões para mover/excluir
            btn_frame = tk.Frame(frame)
            btn_frame.pack(fill="x", pady=5)

            btn_move = tk.Button(btn_frame, text="Mover Selecionadas",
                                 command=lambda grp=group, vars=check_vars: self.move_images(grp, vars))
            btn_move.pack(side="left", padx=5)

            btn_delete = tk.Button(btn_frame, text="Excluir Selecionadas",
                                   command=lambda grp=group, vars=check_vars: self.delete_images(grp, vars))
            btn_delete.pack(side="left", padx=5)

    def move_images(self, group, check_vars):
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return
        for (filepath, _, _), var in zip(group, check_vars):
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
        for (filepath, _, _), var in zip(group, check_vars):
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
