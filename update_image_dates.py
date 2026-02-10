#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from pathlib import Path
from datetime import datetime
from PIL import Image
import piexif

def extract_date_from_filename(filename):
    """
    Extrai a data do nome do arquivo no formato DD.MM.YYYY.
    Ignora tudo após o primeiro espaço.

    Args:
        filename: Nome do arquivo (ex: "02.01.2016 (2).jpg")

    Returns:
        datetime object ou None se não conseguir extrair
    """
    # Remove a extensão e pega apenas o que está antes do primeiro espaço
    name_without_ext = Path(filename).stem
    date_part = name_without_ext.split(' ')[0]

    try:
        # Parse do formato DD.MM.YYYY e define o horário como 15:00:00
        date_obj = datetime.strptime(date_part, "%d.%m.%Y")
        date_obj = date_obj.replace(hour=15, minute=0, second=0)
        return date_obj
    except ValueError as e:
        print(f"⚠️  Não foi possível extrair data de '{filename}': {e}")
        return None

def update_image_metadata(image_path, capture_date):
    """
    Atualiza os metadados EXIF da imagem e a data de modificação do arquivo.

    Args:
        image_path: Caminho completo da imagem
        capture_date: datetime object com a data de captura
    """
    try:
        # Abre a imagem
        img = Image.open(image_path)

        # Formata a data para o formato EXIF (YYYY:MM:DD HH:MM:SS)
        date_str = capture_date.strftime("%Y:%m:%d %H:%M:%S")

        # Carrega EXIF existente ou cria um novo
        try:
            exif_dict = piexif.load(img.info.get('exif', b''))
        except:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        # Atualiza os campos de data
        exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = date_str
        exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = date_str
        exif_dict['0th'][piexif.ImageIFD.DateTime] = date_str

        # Converte para bytes
        exif_bytes = piexif.dump(exif_dict)

        # Salva a imagem com os novos metadados
        img.save(image_path, exif=exif_bytes)
        img.close()

        # Converte a data para timestamp (data de acesso e modificação)
        timestamp = capture_date.timestamp()

        # Atualiza a data de modificação e acesso do arquivo
        # os.utime(path, (atime, mtime))
        os.utime(image_path, (timestamp, timestamp))

        print(f"   📝 Metadados EXIF atualizados")
        print(f"   📅 Data do arquivo atualizada para: {capture_date.strftime('%d/%m/%Y %H:%M:%S')}")

        return True
    except Exception as e:
        print(f"❌ Erro ao processar '{os.path.basename(image_path)}': {e}")
        return False

def process_folder(folder_path):
    """
    Processa todas as imagens na pasta especificada.

    Args:
        folder_path: Caminho da pasta contendo as imagens
    """
    # Extensões de imagem suportadas
    image_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.bmp'}

    folder = Path(folder_path)

    if not folder.exists():
        print(f"❌ A pasta '{folder_path}' não existe!")
        return

    if not folder.is_dir():
        print(f"❌ '{folder_path}' não é uma pasta!")
        return

    # Lista todas as imagens
    images = [f for f in folder.iterdir()
              if f.is_file() and f.suffix.lower() in image_extensions]

    if not images:
        print(f"⚠️  Nenhuma imagem encontrada na pasta '{folder_path}'")
        return

    # Define a data fixa: 01/11/2016 às 15:00:00
    fixed_date = datetime(2016, 11, 1, 15, 0, 0)

    print(f"📁 Processando {len(images)} imagem(ns) em '{folder_path}'...")
    print(f"📅 Todas as imagens serão alteradas para: {fixed_date.strftime('%d/%m/%Y %H:%M:%S')}\n")

    success_count = 0
    failed_count = 0

    for image_file in images:
        print(f"🔄 Processando: {image_file.name}")

        # Atualiza os metadados
        if update_image_metadata(str(image_file), fixed_date):
            print(f"   ✅ Concluído!\n")
            success_count += 1
        else:
            failed_count += 1
            print()

    # Resumo
    print("=" * 50)
    print(f"✅ Imagens processadas com sucesso: {success_count}")
    if failed_count > 0:
        print(f"❌ Imagens com erro: {failed_count}")
    print("=" * 50)

if __name__ == "__main__":
    # Solicita o caminho da pasta ao usuário
    print("=" * 50)
    print("   Atualizador de Datas em Metadados de Imagens")
    print("=" * 50)
    print()

    folder_path = input("Digite o caminho da pasta com as imagens: ").strip()

    # Remove aspas se o usuário copiou o caminho do Windows
    folder_path = folder_path.strip('"').strip("'")

    print()
    process_folder(folder_path)
