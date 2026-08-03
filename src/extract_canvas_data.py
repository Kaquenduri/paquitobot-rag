import json
import os

import requests
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

API_BASE_URL = os.getenv("CANVAS_API_URL", "https://tecsup.instructure.com/api/v1")
API_TOKEN = os.getenv("CANVAS_API_TOKEN")

if not API_TOKEN:
    raise ValueError("Error: Falta CANVAS_API_TOKEN en el archivo .env")

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

def get_paginated_data(url, params=None):
    """
    Realiza peticiones GET manejando la paginación de Canvas a través del encabezado 'Link'.
    """
    if params is None:
        params = {}
    
    # Aseguramos traer la mayor cantidad de elementos por página para reducir peticiones
    params["per_page"] = 100
    
    results = []
    
    while url:
        print(f"Consultando: {url}")
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        # Agregamos los resultados de la página actual
        data = response.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
            
        # Canvas usa el header 'Link' para la paginación
        # Parseamos el header para encontrar el link a la página 'next'
        links = response.links
        if "next" in links:
            url = links["next"]["url"]
            # Limpiamos los params para la siguiente petición ya que la url 'next' ya los incluye
            params = None 
        else:
            url = None
            
    return results

def get_favorite_courses():
    """Obtiene todos los cursos favoritos del dashboard del usuario."""
    url = f"{API_BASE_URL}/users/self/favorites/courses"
    return get_paginated_data(url)

def get_course_assignments(course_id):
    """Obtiene todas las tareas/entregables de un curso específico."""
    url = f"{API_BASE_URL}/courses/{course_id}/assignments"
    # Incluimos información adicional que puede ser útil para las notas (submission)
    params = {
        "include[]": ["submission", "score_statistics"]
    }
    return get_paginated_data(url, params)

def main():
    # Crear directorio para guardar los JSONs si no existe
    output_dir = "src/data"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    print("Obteniendo cursos favoritos...")
    courses = get_favorite_courses()
    print(f"Se encontraron {len(courses)} cursos favoritos.\n")
    
    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "Sin_Nombre")
        
        # Limpiar nombre del curso para usarlo en el nombre del archivo (opcional)
        safe_course_name = "".join([c if c.isalnum() else "_" for c in course_name])
        
        print(f"--- Procesando curso: {course_name} (ID: {course_id}) ---")
        
        try:
            assignments = get_course_assignments(course_id)
            print(f"  -> Se obtuvieron {len(assignments)} entregables/tareas.")
            
            # Limpiamos y estructuramos la información relevante para el RAG
            formatted_assignments = []
            for assignment in assignments:
                submission = assignment.get("submission", {})
                formatted_assign = {
                    "assignment_id": assignment.get("id"),
                    "name": assignment.get("name"),
                    "description": assignment.get("description"), # Puede contener HTML
                    "due_at": assignment.get("due_at"),
                    "points_possible": assignment.get("points_possible"),
                    "html_url": assignment.get("html_url"),
                    "submission_status": submission.get("workflow_state", "unsubmitted"),
                    "score": submission.get("score"),
                    "submitted_at": submission.get("submitted_at")
                }
                formatted_assignments.append(formatted_assign)
                
            # Guardamos los datos del curso en su propio JSON
            course_data = {
                "course_id": course_id,
                "course_name": course_name,
                "assignments": formatted_assignments
            }
            
            filename = os.path.join(output_dir, f"course_{course_id}.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(course_data, f, ensure_ascii=False, indent=4)
                
            print(f"  -> Datos guardados en {filename}")
            
        except requests.exceptions.RequestException as e:
            print(f"Error al obtener datos del curso {course_id}: {e}")

if __name__ == "__main__":
    main()
