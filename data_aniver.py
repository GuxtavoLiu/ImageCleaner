#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from pathlib import Path
from datetime import datetime
from PIL import Image
import piexif

def get_original_date(filepath):
    """
    Extrai a data original dos metadados EXIF da imagem ou da data do arquivo.
    Tenta múltiplas fontes de data para garantir máxima compatibilidade.

    Args:
        filepath: Caminho completo da imagem

    Returns:
        tuple: (datetime object, fonte) onde fonte indica de onde veio a data
    """
    date_found = None
    source = None

    # TENTATIVA 1: Ler EXIF com piexif (mais confiável)
    try:
        exif_dict = piexif.load(filepath)

        # Tenta DateTimeOriginal primeiro
        if piexif.ExifIFD.DateTimeOriginal in exif_dict.get('Exif', {}):
            date_str = exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal].decode('utf-8')
            date_found = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            source = "EXIF DateTimeOriginal"
            return date_found, source

        # Tenta DateTime se DateTimeOriginal não existir
        if piexif.ImageIFD.DateTime in exif_dict.get('0th', {}):
            date_str = exif_dict['0th'][piexif.ImageIFD.DateTime].decode('utf-8')
            date_found = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            source = "EXIF DateTime"
            return date_found, source

    except Exception as e:
        pass  # Continua para próxima tentativa

    # TENTATIVA 2: Ler EXIF com PIL _getexif()
    try:
        img = Image.open(filepath)
        exif_data = img._getexif()

        if exif_data:
            # Tag 36867 = DateTimeOriginal
            date_str = exif_data.get(36867)
            if date_str:
                date_found = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                source = "PIL _getexif"
                return date_found, source
    except Exception as e:
        pass  # Continua para próxima tentativa

    # TENTATIVA 3: Usar data de modificação do arquivo
    try:
        mtime = os.path.getmtime(filepath)
        date_found = datetime.fromtimestamp(mtime)
        source = "Data de Modificação do Arquivo"
        return date_found, source
    except Exception as e:
        pass

    # TENTATIVA 4: Usar data de criação do arquivo (Windows)
    try:
        ctime = os.path.getctime(filepath)
        date_found = datetime.fromtimestamp(ctime)
        source = "Data de Criação do Arquivo"
        return date_found, source
    except Exception as e:
        pass

    return None, None

def update_image_metadata(filepath, new_date):
    """
    Atualiza os metadados EXIF e a data de modificação do arquivo.

    Args:
        filepath: Caminho completo da imagem
        new_date: datetime object com a nova data
    """
    try:
        # Abre a imagem
        img = Image.open(filepath)

        # Formata a data para o formato EXIF (YYYY:MM:DD HH:MM:SS)
        date_str = new_date.strftime("%Y:%m:%d %H:%M:%S")

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
        img.save(filepath, exif=exif_bytes)
        img.close()

        # Converte a data para timestamp (data de acesso e modificação)
        timestamp = new_date.timestamp()

        # Atualiza a data de modificação e acesso do arquivo
        os.utime(filepath, (timestamp, timestamp))

        return True
    except Exception as e:
        print(f"❌ Erro ao processar '{os.path.basename(filepath)}': {e}")
        return False

def process_folder(folder_path):
    """
    Processa todas as imagens .jpg na pasta especificada.
    Converte datas:
    - 29/08/2011 → 19/07/2025 (mantém horário)
    - 30/08/2011 → 20/07/2025 (mantém horário)

    Args:
        folder_path: Caminho da pasta contendo as imagens
    """
    folder = Path(folder_path)

    if not folder.exists():
        print(f"❌ A pasta '{folder_path}' não existe!")
        return

    if not folder.is_dir():
        print(f"❌ '{folder_path}' não é uma pasta!")
        return

    # Lista todos os arquivos .jpg
    images = [f for f in folder.iterdir()
              if f.is_file() and f.suffix.lower() == '.jpg']

    if not images:
        print(f"⚠️  Nenhuma imagem .jpg encontrada na pasta '{folder_path}'")
        return

    print(f"📁 Processando {len(images)} imagem(ns) em '{folder_path}'...\n")
    print("=" * 70)

    success_count = 0
    failed_count = 0
    skipped_count = 0

    # Contadores por conversão
    converted_19_count = 0  # 29/08/2011 → 19/07/2025
    converted_20_count = 0  # 30/08/2011 → 20/07/2025

    for image_file in images:
        print(f"\n🔄 Processando: {image_file.name}")

        # Obtém a data original da imagem
        original_date, source = get_original_date(str(image_file))

        if original_date is None:
            print(f"   ❌ Não foi possível ler a data desta imagem")
            failed_count += 1
            continue

        print(f"   📅 Data original: {original_date.strftime('%d/%m/%Y %H:%M:%S')} (Fonte: {source})")

        # Verifica se a data é 29/08/2011 ou 30/08/2011
        original_date_only = original_date.date()

        new_date = None

        if original_date_only == datetime(2011, 8, 29).date():
            # Converte para 19/07/2025 mantendo o horário
            new_date = datetime(2025, 7, 19,
                              original_date.hour,
                              original_date.minute,
                              original_date.second)
            converted_19_count += 1
            print(f"   🔄 Convertendo: 29/08/2011 → 19/07/2025")

        elif original_date_only == datetime(2011, 8, 30).date():
            # Converte para 20/07/2025 mantendo o horário
            new_date = datetime(2025, 7, 20,
                              original_date.hour,
                              original_date.minute,
                              original_date.second)
            converted_20_count += 1
            print(f"   🔄 Convertendo: 30/08/2011 → 20/07/2025")

        else:
            print(f"   ⏭️  Pulando: Data não corresponde a 29/08/2011 ou 30/08/2011")
            skipped_count += 1
            continue

        # Atualiza os metadados
        print(f"   📅 Nova data: {new_date.strftime('%d/%m/%Y %H:%M:%S')}")

        if update_image_metadata(str(image_file), new_date):
            print(f"   ✅ Concluído!")
            success_count += 1
        else:
            failed_count += 1

    # Resumo
    print("\n" + "=" * 70)
    print("📊 RESUMO DO PROCESSAMENTO")
    print("=" * 70)
    print(f"✅ Imagens processadas com sucesso: {success_count}")
    print(f"   • Convertidas para 19/07/2025: {converted_19_count}")
    print(f"   • Convertidas para 20/07/2025: {converted_20_count}")
    if skipped_count > 0:
        print(f"⏭️  Imagens ignoradas (data diferente): {skipped_count}")
    if failed_count > 0:
        print(f"❌ Imagens com erro: {failed_count}")
    print("=" * 70)

if __name__ == "__main__":
    # Caminho fixo da pasta
    folder_path = r"E:\FOTOS\Aniversário Liu 2025"

    print("=" * 70)
    print("   CONVERSOR DE DATAS - ANIVERSÁRIO LIU 2025")
    print("=" * 70)
    print()
    print(f"📂 Pasta: {folder_path}")
    print()
    print("📋 Conversões programadas:")
    print("   • 29/08/2011 → 19/07/2025 (mantém horário)")
    print("   • 30/08/2011 → 20/07/2025 (mantém horário)")
    print()

    # Confirmação do usuário
    resposta = input("⚠️  Deseja continuar? (S/N): ").strip().upper()

    if resposta == 'S':
        print()
        process_folder(folder_path)
    else:
        print("\n❌ Operação cancelada pelo usuário.")
