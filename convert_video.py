import subprocess
import os
import shutil
from datetime import datetime

# Caminho do ffmpeg
ffmpeg_path = r"C:\ffmpeg\bin\ffmpeg.exe"

# Caminho do vídeo original
video_path = r"F:\Acervo de Fotos\Organizadas por Ano\2006\videos\AGO+SET-2006 026.mpg"

# Gera o caminho de saída (mesmo nome, extensão .mp4)
output_path = os.path.splitext(video_path)[0] + ".mp4"

print(f"Convertendo: {video_path}")
print(f"Para: {output_path}")

# Verifica se o arquivo existe
if not os.path.exists(video_path):
    print(f"ERRO: Arquivo não encontrado: {video_path}")
    exit(1)

# Captura os timestamps originais do arquivo
original_mtime = os.path.getmtime(video_path)
original_atime = os.path.getatime(video_path)

try:
    # Verifica se ffmpeg está disponível
    if not os.path.exists(ffmpeg_path):
        print(f"ERRO: ffmpeg não encontrado em: {ffmpeg_path}")
        print("Verifique se o caminho está correto.")
        exit(1)

    # Converte o vídeo preservando metadados
    # -i: arquivo de entrada
    # -map_metadata 0: copia todos os metadados do arquivo de entrada
    # -c:v libx264: codec de vídeo H.264 (padrão MP4)
    # -c:a aac: codec de áudio AAC (padrão MP4)
    # -movflags use_metadata_tags: preserva tags de metadados
    # -crf 18: qualidade alta (0-51, menor é melhor)

    command = [
        ffmpeg_path,
        '-i', video_path,
        '-map_metadata', '0',
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '18',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-movflags', 'use_metadata_tags',
        '-y',  # sobrescreve se já existir
        output_path
    ]

    print("\nIniciando conversão...")
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode == 0:
        print("✓ Conversão concluída com sucesso!")

        # Restaura os timestamps originais no arquivo convertido
        os.utime(output_path, (original_atime, original_mtime))

        # Mostra informações
        original_size = os.path.getsize(video_path) / (1024 * 1024)
        converted_size = os.path.getsize(output_path) / (1024 * 1024)
        original_date = datetime.fromtimestamp(original_mtime).strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n📊 Informações:")
        print(f"   Tamanho original: {original_size:.2f} MB")
        print(f"   Tamanho convertido: {converted_size:.2f} MB")
        print(f"   Data de modificação preservada: {original_date}")
    else:
        print("✗ Erro na conversão!")
        print(f"Erro: {result.stderr}")
        exit(1)

except Exception as e:
    print(f"ERRO: {e}")
    exit(1)

print("\n✓ Processo concluído!")
