import argparse
import os
import sys
from dotenv import load_dotenv
from roboflow import Roboflow

load_dotenv()

def main():
    parser = argparse.ArgumentParser(
        description="Script MLOps para sincronización y subida de datasets curados a Roboflow."
    )
    parser.add_argument(
        "--folder",
        type=str,
        required=True,
        help="Ruta (relativa o absoluta) a la carpeta con imágenes (.jpg), anotaciones (.txt) y classes.txt"
    )
    parser.add_argument(
        "--batch-name",
        type=str,
        default=None,
        help="Nombre opcional para identificar el lote en Roboflow. Si se omite, se genera dinámicamente."
    )
    args = parser.parse_args()

    api_key = os.getenv("ROBOFLOW_API_KEY")
    workspace_id = os.getenv("ROBOFLOW_WORKSPACE")
    project_id = os.getenv("ROBOFLOW_PROJECT")

    faltantes = []
    if not api_key: faltantes.append("ROBOFLOW_API_KEY")
    if not workspace_id: faltantes.append("ROBOFLOW_WORKSPACE")
    if not project_id: faltantes.append("ROBOFLOW_PROJECT")

    if faltantes:
        print(f"[-] ERROR CRÍTICO: Faltan las siguientes variables en el entorno (.env): {', '.join(faltantes)}")
        print("[-] Regístralas en tu archivo .env antes de ejecutar este script.")
        sys.exit(1)

    ruta_carpeta = os.path.abspath(os.path.expanduser(args.folder))

    if not os.path.exists(ruta_carpeta):
        print(f"[-] ERROR: La ruta especificada no existe: {ruta_carpeta}")
        sys.exit(1)

    if not os.path.isdir(ruta_carpeta):
        print(f"[-] ERROR: La ruta indicada no corresponde a un directorio: {ruta_carpeta}")
        sys.exit(1)

    archivos_directorio = os.listdir(ruta_carpeta)
    imagenes = [f for f in archivos_directorio if f.lower().endswith(('.jpg', '.jpeg', '.png')) and not f.lower().endswith('_vis.jpg')]
    etiquetas = [f for f in archivos_directorio if f.lower().endswith('.txt') and f != 'classes.txt' and not f.lower().endswith('_vis.txt')]
    tiene_classes_txt = "classes.txt" in archivos_directorio

    print("\n" + "=" * 60)
    print("    SCANNOR QA - PIPELINE DE SUBIDA A ROBOFLOW")
    print("=" * 60)
    print(f"[*] Directorio origen : {ruta_carpeta}")
    print(f"[*] Imágenes listas   : {len(imagenes)}")
    print(f"[*] Archivos .txt     : {len(etiquetas)}")
    print(f"[*] Archivo classes   : {'Detectado (Correcto)' if tiene_classes_txt else 'NO DETECTADO (Advertencia: puede asignar IDs numéricos)'}")
    print(f"[*] Workspace destino : {workspace_id}")
    print(f"[*] Proyecto destino  : {project_id}")

    if len(imagenes) == 0:
        print("[-] No se hallaron imágenes en la carpeta. Operación abortada.")
        sys.exit(0)

    nombre_lote = args.batch_name or f"curado_{os.path.basename(ruta_carpeta)}"

    try:
        print(f"\n[*] Autenticando con el SDK de Roboflow...")
        rf = Roboflow(api_key=api_key)
        workspace = rf.workspace(workspace_id)
        project = workspace.project(project_id)

        print(f"[*] Subiendo lote '{nombre_lote}' iterando archivos...")
        
        for img_name in imagenes:
            img_path = os.path.join(ruta_carpeta, img_name)
            txt_name = img_name.rsplit('.', 1)[0] + '.txt'
            txt_path = os.path.join(ruta_carpeta, txt_name)
            
            if os.path.exists(txt_path):
                print(f"  -> Subiendo: {img_name} + {txt_name}")
                project.upload(
                    image_path=img_path,
                    annotation_path=txt_path,
                    num_retry_uploads=3,
                    batch_name=nombre_lote
                )
            else:
                print(f"  -> Subiendo: {img_name} (Sin defectos / Etiqueta vacía)")
                project.upload(
                    image_path=img_path,
                    num_retry_uploads=3,
                    batch_name=nombre_lote
                )

        print("[+] ¡Sincronización completada exitosamente!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"[-] Error durante el procesamiento con Roboflow: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()