import os
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from PIL import Image, ImageTk, ImageFile
import imagehash
from datetime import datetime

# Permite carregar imagens truncadas/corrompidas parcialmente
ImageFile.LOAD_TRUNCATED_IMAGES = True

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
        self.current_page = 0
        self.groups_per_page = 10
        self.group_check_vars = {}  # Armazena check_vars por grupo
        self.scan_errors = []  # Armazena erros de escaneamento
        self.create_widgets()

    def create_widgets(self):
        self.select_btn = tk.Button(self.master, text="Selecionar Pasta", command=self.select_folder)
        self.select_btn.pack(pady=10)

        # Label para exibir o caminho selecionado
        self.path_label = tk.Label(self.master, text="", fg="blue", wraplength=400)
        self.path_label.pack(pady=5)

        # Frame para checkbox de subpastas (inicialmente oculto)
        self.subfolder_frame = tk.Frame(self.master)

        # Checkbox para escanear subpastas (marcada por padrão)
        self.scan_subfolders_var = tk.IntVar(value=1)
        self.subfolder_check = tk.Checkbutton(
            self.subfolder_frame,
            text="Escanear subpastas",
            variable=self.scan_subfolders_var
        )
        self.subfolder_check.pack(side="left")

        # Ícone de informação (tooltip)
        self.info_label = tk.Label(self.subfolder_frame, text="ℹ️", fg="blue", cursor="hand2")
        self.info_label.pack(side="left", padx=5)

        # Binds para o tooltip
        self.create_tooltip(self.info_label,
                           "Se marcado, o programa irá escanear a pasta selecionada\n"
                           "e todas as suas subpastas recursivamente.\n"
                           "Se desmarcado, apenas a pasta raiz será escaneada.")

        # Botão Iniciar (inicialmente oculto)
        self.start_btn = tk.Button(self.master, text="Iniciar", command=self.start_scan)
        # Não exibe o botão nem o frame de subpastas inicialmente

    def create_tooltip(self, widget, text):
        """Cria um tooltip para um widget"""
        def on_enter(event):
            tooltip = tk.Toplevel()
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")

            label = tk.Label(tooltip, text=text, justify='left',
                           background="#ffffe0", relief='solid', borderwidth=1,
                           font=("Arial", 9))
            label.pack()

            widget.tooltip = tooltip

        def on_leave(event):
            if hasattr(widget, 'tooltip'):
                widget.tooltip.destroy()
                del widget.tooltip

        widget.bind('<Enter>', on_enter)
        widget.bind('<Leave>', on_leave)

    def select_folder(self):
        folder = filedialog.askdirectory(title="Selecione a pasta com imagens")
        if folder:
            self.selected_folder = folder
            self.path_label.config(text=f"Pasta selecionada: {folder}")
            self.subfolder_frame.pack(pady=5)
            self.start_btn.pack(pady=10)

    def start_scan(self):
        """Inicia o escaneamento quando o usuário clicar no botão Iniciar"""
        if self.selected_folder:
            self.create_progress_window()
            # Agenda o scan para depois que a janela for criada
            self.master.after(100, self.scan_folder)

    def create_progress_window(self):
        """Cria janela de progresso"""
        self.progress_window = tk.Toplevel(self.master)
        self.progress_window.title("Escaneando Imagens")
        self.progress_window.geometry("500x150")
        self.progress_window.resizable(False, False)

        # Centraliza a janela
        self.progress_window.transient(self.master)
        self.progress_window.grab_set()

        # Frame principal
        main_frame = tk.Frame(self.progress_window, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)

        # Label de status
        self.progress_label = tk.Label(main_frame, text="Inicializando...", font=("Arial", 10))
        self.progress_label.pack(pady=(0, 10))

        # Barra de progresso
        self.progress_bar = ttk.Progressbar(main_frame, length=450, mode='determinate')
        self.progress_bar.pack(pady=10)

        # Label de contagem
        self.progress_count_label = tk.Label(main_frame, text="0 / 0 imagens", font=("Arial", 9))
        self.progress_count_label.pack(pady=(5, 0))

    def update_progress(self, current, total, filename):
        """Atualiza a barra de progresso"""
        if hasattr(self, 'progress_window') and self.progress_window.winfo_exists():
            # Atualiza o progresso
            progress_percent = (current / total * 100) if total > 0 else 0
            self.progress_bar['value'] = progress_percent

            # Atualiza labels
            self.progress_label.config(text=f"Processando: {os.path.basename(filename)}")
            self.progress_count_label.config(text=f"{current} / {total} imagens")

            # Força atualização da interface
            self.progress_window.update()

    def scan_folder(self):
        self.images_data = []
        self.scan_errors = []  # Reseta lista de erros
        valid_extensions = [".jpg", ".jpeg", ".png", ".bmp", ".gif"]

        total_files = 0
        processed_files = 0

        # Verifica se deve escanear subpastas
        scan_subfolders = self.scan_subfolders_var.get() == 1

        # Primeira passagem: contar total de arquivos
        self.progress_label.config(text="Contando arquivos...")
        self.progress_window.update()

        file_list = []
        if scan_subfolders:
            for root, dirs, files in os.walk(self.selected_folder):
                for file in files:
                    if os.path.splitext(file)[1].lower() in valid_extensions:
                        file_list.append(os.path.join(root, file))
        else:
            try:
                files = os.listdir(self.selected_folder)
                for file in files:
                    filepath = os.path.join(self.selected_folder, file)
                    if os.path.isfile(filepath) and os.path.splitext(file)[1].lower() in valid_extensions:
                        file_list.append(filepath)
            except Exception as e:
                if hasattr(self, 'progress_window'):
                    self.progress_window.destroy()
                messagebox.showerror("Erro", f"Erro ao listar arquivos: {e}")
                return

        total_files = len(file_list)

        if total_files == 0:
            if hasattr(self, 'progress_window'):
                self.progress_window.destroy()
            messagebox.showinfo("Resultado", "Nenhuma imagem encontrada.")
            return

        # Segunda passagem: processar arquivos
        def process_image(filepath):
            """Processa uma imagem e categoriza erros se houver"""
            nonlocal processed_files
            try:
                # Calcula perceptual hash
                with Image.open(filepath) as img:
                    hash_val = imagehash.phash(img)
                # Calcula MD5 para detectar arquivos idênticos
                md5_val = get_file_md5(filepath)
                # Armazena tupla com (caminho, p-hash, md5)
                self.images_data.append((filepath, hash_val, md5_val))
                processed_files += 1
                return True
            except Exception as e:
                # Categoriza o erro
                error_type = "Desconhecido"
                error_msg = str(e)

                if "truncated" in error_msg.lower():
                    error_type = "Arquivo Truncado"
                    error_msg = "Imagem incompleta ou corrompida (dados faltando)"
                elif "broken data stream" in error_msg.lower():
                    error_type = "Dados Corrompidos"
                    error_msg = "Fluxo de dados da imagem está quebrado"
                elif "cannot identify image file" in error_msg.lower():
                    error_type = "Formato Inválido"
                    error_msg = "Arquivo não é uma imagem válida ou formato não suportado"
                elif "permission" in error_msg.lower():
                    error_type = "Sem Permissão"
                    error_msg = "Sem permissão para ler o arquivo"
                else:
                    error_msg = str(e)

                self.scan_errors.append({
                    'filepath': filepath,
                    'type': error_type,
                    'message': error_msg
                })
                processed_files += 1  # Conta mesmo com erro
                return False

        # Processa cada arquivo com atualização de progresso
        for idx, filepath in enumerate(file_list, 1):
            process_image(filepath)
            self.update_progress(idx, total_files, filepath)

        # Fecha janela de progresso
        if hasattr(self, 'progress_window') and self.progress_window.winfo_exists():
            self.progress_window.destroy()

        # Exibe resumo do escaneamento
        self.show_scan_summary(total_files, processed_files)

        # Ajuste o threshold conforme necessário
        self.group_images(threshold=10)

    def show_scan_summary(self, total_files, processed_files):
        """Exibe resumo do escaneamento com detalhes de erros"""
        if not self.scan_errors:
            # Sem erros
            message = (
                f"✓ {processed_files} de {total_files} imagens processadas com sucesso!\n\n"
                f"⚠️ Ao clicar em OK, o carregamento pode demorar alguns minutos.\n"
                f"Por favor, aguarde."
            )
            messagebox.showinfo("Escaneamento Concluído", message)
            return

        # Há erros - mostra janela detalhada
        error_window = tk.Toplevel(self.master)
        error_window.title("Relatório de Escaneamento")
        error_window.geometry("700x500")

        # Torna a janela modal
        error_window.transient(self.master)
        error_window.grab_set()

        # Frame superior com resumo
        summary_frame = tk.Frame(error_window, bg="#fff3cd", padx=10, pady=10)
        summary_frame.pack(fill="x", padx=10, pady=10)

        success_count = processed_files
        error_count = len(self.scan_errors)

        summary_text = (
            f"✓ Imagens processadas: {success_count}\n"
            f"✗ Imagens com erro: {error_count}\n"
            f"📊 Total encontrado: {total_files}"
        )

        tk.Label(summary_frame, text=summary_text, font=("Arial", 10, "bold"),
                bg="#fff3cd", justify="left").pack(anchor="w")

        # Aviso sobre tempo de carregamento
        warning_frame = tk.Frame(error_window, bg="#fff3cd", padx=10)
        warning_frame.pack(fill="x", padx=10, pady=(0, 10))

        warning_text = "⚠️ Ao fechar esta janela, o carregamento pode demorar alguns minutos. Por favor, aguarde."
        tk.Label(warning_frame, text=warning_text, font=("Arial", 9),
                bg="#fff3cd", fg="#856404", justify="left").pack(anchor="w")

        # Categoriza erros
        error_types = {}
        for error in self.scan_errors:
            error_type = error['type']
            if error_type not in error_types:
                error_types[error_type] = []
            error_types[error_type].append(error)

        # Frame com categorias
        categories_frame = tk.Frame(error_window, padx=10)
        categories_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(categories_frame, text="Tipos de Erro Encontrados:",
                font=("Arial", 9, "bold")).pack(anchor="w")

        for error_type, errors in error_types.items():
            tk.Label(categories_frame, text=f"  • {error_type}: {len(errors)} arquivo(s)",
                    font=("Arial", 9)).pack(anchor="w")

        # Lista de erros em scrolled text
        tk.Label(error_window, text="Detalhes dos Erros:", font=("Arial", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        text_frame = tk.Frame(error_window)
        text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        error_text = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, font=("Courier", 8))
        error_text.pack(fill="both", expand=True)

        # Agrupa por tipo
        for error_type, errors in error_types.items():
            error_text.insert(tk.END, f"\n═══ {error_type} ({len(errors)}) ═══\n", "header")
            for error in errors:
                error_text.insert(tk.END, f"\n📁 Arquivo: {error['filepath']}\n")
                error_text.insert(tk.END, f"💬 Detalhes: {error['message']}\n")

        # Configurações de tag
        error_text.tag_config("header", font=("Courier", 9, "bold"), foreground="#d9534f")
        error_text.config(state="disabled")

        # Frame inferior com botões e informações
        bottom_frame = tk.Frame(error_window)
        bottom_frame.pack(fill="x", padx=10, pady=10)

        # Informações úteis
        info_frame = tk.LabelFrame(bottom_frame, text="O que fazer?", padx=10, pady=10)
        info_frame.pack(fill="x", pady=(0, 10))

        info_text = (
            "• Arquivo Truncado: Imagem incompleta, possivelmente download interrompido\n"
            "• Dados Corrompidos: Arquivo danificado, pode estar corrompido no disco\n"
            "• Formato Inválido: Extensão .jpg mas não é uma imagem válida\n\n"
            "💡 Recomendação: Você pode tentar recuperar essas imagens com ferramentas\n"
            "   especializadas ou movê-las para uma pasta separada para análise manual."
        )

        tk.Label(info_frame, text=info_text, justify="left", font=("Arial", 8)).pack(anchor="w")

        # Botão fechar
        tk.Button(bottom_frame, text="Fechar", command=error_window.destroy,
                 bg="#5cb85c", fg="white", padx=20).pack()

        # Aguarda o usuário fechar a janela antes de continuar
        error_window.wait_window()

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

    def create_groups_progress_window(self):
        """Cria janela de progresso para inicialização de grupos"""
        self.groups_progress_window = tk.Toplevel(self.master)
        self.groups_progress_window.title("Preparando Grupos")
        self.groups_progress_window.geometry("500x150")
        self.groups_progress_window.resizable(False, False)

        # Centraliza a janela
        self.groups_progress_window.transient(self.master)
        self.groups_progress_window.grab_set()

        # Frame principal
        main_frame = tk.Frame(self.groups_progress_window, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)

        # Label de status
        self.groups_progress_label = tk.Label(main_frame, text="Inicializando grupos...", font=("Arial", 10))
        self.groups_progress_label.pack(pady=(0, 10))

        # Barra de progresso
        self.groups_progress_bar = ttk.Progressbar(main_frame, length=450, mode='determinate')
        self.groups_progress_bar.pack(pady=10)

        # Label de contagem
        self.groups_progress_count_label = tk.Label(main_frame, text="0 / 0 grupos", font=("Arial", 9))
        self.groups_progress_count_label.pack(pady=(5, 0))

        # Força atualização
        self.groups_progress_window.update()

    def update_groups_progress(self, current, total):
        """Atualiza a barra de progresso de grupos"""
        if hasattr(self, 'groups_progress_window') and self.groups_progress_window.winfo_exists():
            # Atualiza o progresso
            progress_percent = (current / total * 100) if total > 0 else 0
            self.groups_progress_bar['value'] = progress_percent

            # Atualiza labels
            self.groups_progress_label.config(text=f"Preparando grupo {current} de {total}...")
            self.groups_progress_count_label.config(text=f"{current} / {total} grupos")

            # Força atualização da interface
            self.groups_progress_window.update()

    def initialize_all_groups(self):
        """Inicializa estrutura de dados para todos os grupos antes de renderizar"""
        total_groups = len(self.groups)

        for idx, group in enumerate(self.groups):
            # Atualiza progresso
            self.update_groups_progress(idx + 1, total_groups)

            check_vars = []
            image_info_list = []

            # Conta MD5 duplicados
            md5_count = {}
            for (_, _, md5_val) in group:
                md5_count[md5_val] = md5_count.get(md5_val, 0) + 1

            # Cria IntVar para cada imagem
            for filepath, p_hash, md5_val in group:
                var = tk.IntVar()
                check_vars.append(var)

                image_info_list.append({
                    'filepath': filepath,
                    'md5': md5_val,
                    'var': var,
                    'mtime': os.path.getmtime(filepath)
                })

            # Armazena dados do grupo
            self.group_check_vars[idx] = {
                'check_vars': check_vars,
                'images': image_info_list,
                'md5_count': md5_count,
                'group': group
            }

    def show_groups(self):
        """Exibe a janela listando os grupos de imagens com opções de mover ou excluir,
           dentro de um canvas com scrollbar e paginação."""
        self.current_page = 0
        self.group_check_vars = {}  # Reseta a estrutura

        # Cria janela de progresso
        self.create_groups_progress_window()

        # Inicializa estrutura de dados para TODOS os grupos
        self.initialize_all_groups()

        # Fecha janela de progresso
        if hasattr(self, 'groups_progress_window') and self.groups_progress_window.winfo_exists():
            self.groups_progress_window.destroy()

        self.groups_window = tk.Toplevel(self.master)
        self.groups_window.title("Grupos de Imagens Similares")

        # Frame superior com informações e navegação
        top_frame = tk.Frame(self.groups_window)
        top_frame.pack(fill="x", padx=10, pady=5)

        # Label com informação de paginação
        self.page_info_label = tk.Label(top_frame, text="", font=("Arial", 10))
        self.page_info_label.pack(side="left", padx=5)

        # Botão para selecionar idênticas
        btn_select_identical = tk.Button(top_frame, text="Selecionar Idênticas",
                                         command=self.select_identical_images,
                                         bg="#4CAF50", fg="white")
        btn_select_identical.pack(side="left", padx=5)

        # Botão para selecionar semelhantes
        btn_select_similar = tk.Button(top_frame, text="Selecionar Semelhantes",
                                       command=self.select_similar_images,
                                       bg="#FF9800", fg="white")
        btn_select_similar.pack(side="left", padx=5)

        # Botões de ação global
        btn_move_all = tk.Button(top_frame, text="Mover Todas Selecionadas",
                                command=self.move_all_selected,
                                bg="#2196F3", fg="white")
        btn_move_all.pack(side="left", padx=5)

        btn_delete_all = tk.Button(top_frame, text="Excluir Todas Selecionadas",
                                   command=self.delete_all_selected,
                                   bg="#f44336", fg="white")
        btn_delete_all.pack(side="left", padx=5)

        # Botões de navegação
        nav_frame = tk.Frame(top_frame)
        nav_frame.pack(side="right")

        self.prev_btn = tk.Button(nav_frame, text="← Anterior", command=self.prev_page)
        self.prev_btn.pack(side="left", padx=5)

        self.next_btn = tk.Button(nav_frame, text="Próximo →", command=self.next_page)
        self.next_btn.pack(side="left", padx=5)

        # --- Cria um Frame para conter o Canvas e a Scrollbar ---
        scroll_container = tk.Frame(self.groups_window)
        scroll_container.pack(fill="both", expand=True)

        # --- Cria o Canvas ---
        self.canvas = tk.Canvas(scroll_container)
        self.canvas.pack(side="left", fill="both", expand=True)

        # --- Cria a Scrollbar e vincula ao Canvas ---
        scrollbar = tk.Scrollbar(scroll_container, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # --- Cria o Frame que conterá todo o conteúdo (grupos, imagens, etc.) ---
        self.content_frame = tk.Frame(self.canvas)
        # Insere o content_frame dentro do canvas como uma "janela"
        self.canvas.create_window((0, 0), window=self.content_frame, anchor="nw")

        # Função para ajustar a região de rolagem sempre que o content_frame mudar de tamanho
        def on_configure(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self.content_frame.bind("<Configure>", on_configure)

        # Adiciona suporte ao scroll do mouse
        def on_mouse_wheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Bind para Windows/MacOS
        self.canvas.bind_all("<MouseWheel>", on_mouse_wheel)
        # Bind para Linux
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        # Renderiza a primeira página
        self.render_page()

    def render_page(self):
        """Renderiza os grupos da página atual usando dados já inicializados"""
        # Limpa o conteúdo anterior
        for widget in self.content_frame.winfo_children():
            widget.destroy()

        # Calcula índices da página atual
        start_idx = self.current_page * self.groups_per_page
        end_idx = min(start_idx + self.groups_per_page, len(self.groups))
        total_pages = (len(self.groups) + self.groups_per_page - 1) // self.groups_per_page

        # Atualiza label de informação
        self.page_info_label.config(
            text=f"Página {self.current_page + 1} de {total_pages} | Total de grupos: {len(self.groups)}"
        )

        # Atualiza estado dos botões
        self.prev_btn.config(state="normal" if self.current_page > 0 else "disabled")
        self.next_btn.config(state="normal" if end_idx < len(self.groups) else "disabled")

        # Renderiza grupos da página atual
        for idx in range(start_idx, end_idx):
            group_data = self.group_check_vars[idx]
            group = group_data['group']
            md5_count = group_data['md5_count']
            images = group_data['images']

            frame = tk.LabelFrame(self.content_frame, text=f"Grupo {idx + 1}", padx=10, pady=10)
            frame.pack(padx=10, pady=10, fill="x", expand=True)

            # Exibe cada imagem do grupo usando os IntVar já criados
            for img_info in images:
                filepath = img_info['filepath']
                md5_val = img_info['md5']
                var = img_info['var']

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

                # Checkbutton usando o IntVar já existente
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

            # Botões para mover/excluir do grupo específico
            btn_frame = tk.Frame(frame)
            btn_frame.pack(fill="x", pady=5)

            btn_move = tk.Button(btn_frame, text="Mover Selecionadas",
                                 command=lambda grp=group, vars=group_data['check_vars']: self.move_images(grp, vars))
            btn_move.pack(side="left", padx=5)

            btn_delete = tk.Button(btn_frame, text="Excluir Selecionadas",
                                   command=lambda grp=group, vars=group_data['check_vars']: self.delete_images(grp, vars))
            btn_delete.pack(side="left", padx=5)

        # Reseta o scroll para o topo
        self.canvas.yview_moveto(0)

    def prev_page(self):
        """Navega para a página anterior"""
        if self.current_page > 0:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        """Navega para a próxima página"""
        total_pages = (len(self.groups) + self.groups_per_page - 1) // self.groups_per_page
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.render_page()

    def select_identical_images(self):
        """Seleciona automaticamente imagens idênticas (mesmo MD5),
           deixando apenas a mais antiga de cada grupo não selecionada."""
        selected_count = 0

        # Itera sobre todos os grupos da página atual
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']
            md5_count = group_data['md5_count']

            # Agrupa imagens por MD5
            md5_groups = {}
            for img_info in images:
                md5 = img_info['md5']
                if md5 not in md5_groups:
                    md5_groups[md5] = []
                md5_groups[md5].append(img_info)

            # Para cada MD5 que aparece mais de uma vez (idênticas)
            for md5, identical_images in md5_groups.items():
                if len(identical_images) > 1:
                    # Ordena por data de modificação (mais antiga primeiro)
                    identical_images.sort(key=lambda x: x['mtime'])

                    # Seleciona todas exceto a primeira (mais antiga)
                    for img_info in identical_images[1:]:
                        img_info['var'].set(1)
                        selected_count += 1

        messagebox.showinfo("Seleção Concluída",
                           f"{selected_count} imagens idênticas foram selecionadas (mantendo a mais antiga de cada grupo).")

    def select_similar_images(self):
        """Seleciona automaticamente imagens semelhantes (MD5 diferente),
           deixando apenas a mais antiga de cada grupo não selecionada."""
        selected_count = 0

        # Itera sobre todos os grupos
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']
            md5_count = group_data['md5_count']

            # Filtra apenas imagens semelhantes (MD5 único, ou seja, não duplicado)
            similar_images = [img_info for img_info in images if md5_count[img_info['md5']] == 1]

            # Se há pelo menos 2 imagens semelhantes no grupo
            if len(similar_images) > 1:
                # Ordena por data de modificação (mais antiga primeiro)
                similar_images.sort(key=lambda x: x['mtime'])

                # Seleciona todas exceto a primeira (mais antiga)
                for img_info in similar_images[1:]:
                    img_info['var'].set(1)
                    selected_count += 1

        messagebox.showinfo("Seleção Concluída",
                           f"{selected_count} imagens semelhantes foram selecionadas (mantendo a mais antiga de cada grupo).")

    def move_all_selected(self):
        """Move todas as imagens selecionadas de todos os grupos"""
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return

        moved_count = 0
        errors = []

        # Itera sobre todos os grupos
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']

            # Move cada imagem selecionada
            for img_info in images:
                if img_info['var'].get() == 1:
                    filepath = img_info['filepath']
                    try:
                        basename = os.path.basename(filepath)
                        new_path = os.path.join(dest_folder, basename)

                        # Se arquivo já existe no destino, adiciona sufixo
                        if os.path.exists(new_path):
                            name, ext = os.path.splitext(basename)
                            counter = 1
                            while os.path.exists(new_path):
                                new_path = os.path.join(dest_folder, f"{name}_{counter}{ext}")
                                counter += 1

                        os.rename(filepath, new_path)
                        moved_count += 1
                        img_info['var'].set(0)  # Desmarca após mover
                    except Exception as e:
                        errors.append(f"{filepath}: {str(e)}")

        # Recarrega a página atual para atualizar a visualização
        self.render_page()

        if errors:
            error_msg = f"{moved_count} imagens movidas.\n\nErros:\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                error_msg += f"\n... e mais {len(errors) - 5} erros."
            messagebox.showwarning("Mover - Concluído com Erros", error_msg)
        else:
            messagebox.showinfo("Mover", f"{moved_count} imagens movidas com sucesso!")

    def delete_all_selected(self):
        """Exclui todas as imagens selecionadas de todos os grupos"""
        # Conta quantas imagens estão selecionadas
        selected_count = sum(1 for group_data in self.group_check_vars.values()
                           for img_info in group_data['images']
                           if img_info['var'].get() == 1)

        if selected_count == 0:
            messagebox.showinfo("Excluir", "Nenhuma imagem selecionada.")
            return

        confirm = messagebox.askyesno("Excluir",
                                     f"Tem certeza que deseja excluir {selected_count} imagens selecionadas?")
        if not confirm:
            return

        deleted_count = 0
        errors = []

        # Itera sobre todos os grupos
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']

            # Exclui cada imagem selecionada
            for img_info in images:
                if img_info['var'].get() == 1:
                    filepath = img_info['filepath']
                    try:
                        os.remove(filepath)
                        deleted_count += 1
                        img_info['var'].set(0)  # Desmarca após excluir
                    except Exception as e:
                        errors.append(f"{filepath}: {str(e)}")

        # Recarrega a página atual para atualizar a visualização
        self.render_page()

        if errors:
            error_msg = f"{deleted_count} imagens excluídas.\n\nErros:\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                error_msg += f"\n... e mais {len(errors) - 5} erros."
            messagebox.showwarning("Excluir - Concluído com Erros", error_msg)
        else:
            messagebox.showinfo("Excluir", f"{deleted_count} imagens excluídas com sucesso!")

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
