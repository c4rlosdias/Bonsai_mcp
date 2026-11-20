import bpy
import mathutils
import json
import threading
import socket
import time
import requests
import tempfile
import traceback
import os
import shutil
from bpy.props import StringProperty, IntProperty, BoolProperty, EnumProperty
import base64

import bpy

import ifcopenshell
import ifcopenshell.util.element


class _LazyIfcStore:
    """Resolve bonsai's IfcStore on first use instead of at import time.

    Blender loads legacy add-ons before it registers extensions, so importing
    ``bonsai.bim`` here would fail (Bonsai sets REGISTERED_BBIM_PACKAGE during
    its own registration) and abort this add-on's registration entirely.
    """

    def __getattr__(self, name):
        from bonsai.bim.ifc import IfcStore as _IfcStore
        return getattr(_IfcStore, name)


IfcStore = _LazyIfcStore()

bl_info = {
    "name": "Bonsai MCP",
    "author": "JotaDeRodriguez",
    "version": (0, 5),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Bonsai MCP",
    "description": "Connect Claude to Blender via MCP. Aimed at IFC projects",
    "category": "Interface",
}


class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
    
    def start(self):
        if self.running:
            print("Server is already running")
            return
            
        self.running = True
        
        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            
            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            
    def stop(self):
        self.running = False
        
        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        
        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None
        
        print("BlenderMCP server stopped")
    
    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping
        
        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)
        
        print("Server thread stopped")
    
    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        client.settimeout(None)  # No timeout
        buffer = b''
        
        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break
                    
                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''
                        
                        # Execute command in Blender's main thread
                        def execute_wrapper():
                            try:
                                response = self.execute_command(command)
                                response_json = json.dumps(response)
                                try:
                                    client.sendall(response_json.encode('utf-8'))
                                except:
                                    print("Failed to send response - client disconnected")
                            except Exception as e:
                                print(f"Error executing command: {str(e)}")
                                traceback.print_exc()
                                try:
                                    error_response = {
                                        "status": "error",
                                        "message": str(e)
                                    }
                                    client.sendall(json.dumps(error_response).encode('utf-8'))
                                except:
                                    pass
                            return None
                        
                        # Schedule execution in main thread
                        bpy.app.timers.register(execute_wrapper, first_interval=0.0)
                    except json.JSONDecodeError:
                        # Incomplete data, wait for more
                        pass
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            
            # Ensure we're in the right context
            if cmd_type in ["create_object", "modify_object", "delete_object"]:
                override = bpy.context.copy()
                override['area'] = [area for area in bpy.context.screen.areas if area.type == 'VIEW_3D'][0]
                with bpy.context.temp_override(**override):
                    return self._execute_command_internal(command)
            else:
                return self._execute_command_internal(command)
                
        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        
        # Base handlers that are always available
        handlers = {
            "execute_code": self.execute_code,
            "get_ifc_project_info": self.get_ifc_project_info,
            "list_ifc_entities": self.list_ifc_entities,
            "get_ifc_properties": self.get_ifc_properties,
            "get_ifc_spatial_structure": self.get_ifc_spatial_structure,
            "get_ifc_total_structure": self.get_ifc_total_structure,
            "get_ifc_relationships": self.get_ifc_relationships,
            "get_selected_ifc_entities": self.get_selected_ifc_entities,
            "get_current_view": self.get_current_view,
            "export_ifc_data": self.export_ifc_data,
            "place_ifc_object": self.place_ifc_object,
            "get_ifc_quantities": self.get_ifc_quantities,
            "export_drawing_png": self.export_drawing_png,
            "get_ifc_georeferencing_info": self.get_ifc_georeferencing_info,
            "georeference_ifc_model": self.georeference_ifc_model,
            "generate_ids": self.generate_ids,
            # geometry analysis & temporary drawings
            "get_element_geometry": get_element_geometry,
            "measure_distance": measure_distance,
            "detect_clashes": detect_clashes,
            "draw_dimension": draw_dimension,
            "draw_bounding_boxes": draw_bounding_boxes,
            "clear_temporary_drawings": clear_temporary_drawings,
            # space boundaries
            "get_space_boundaries": get_space_boundaries,
            "validate_space_boundaries": validate_space_boundaries,
            "draw_space_boundaries": draw_space_boundaries,
            "suggest_space_boundaries": suggest_space_boundaries,
            "create_space_boundaries": create_space_boundaries,
            "remove_space_boundaries": remove_space_boundaries,
            "fix_space_boundary_attributes": fix_space_boundary_attributes,
        }
        

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    
    def execute_code(self, code):
        """Execute arbitrary Blender Python code.

        Everything printed with print() is returned in "output". If the code
        defines a variable named ``result`` it is returned as well.
        """
        import io
        import contextlib
        buffer = io.StringIO()
        namespace = {"bpy": bpy}
        try:
            with contextlib.redirect_stdout(buffer):
                exec(code, namespace)
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}\n--- output before error ---\n{buffer.getvalue()}")
        response = {"executed": True, "output": buffer.getvalue()}
        if "result" in namespace:
            value = namespace["result"]
            try:
                json.dumps(value)
                response["result"] = value
            except (TypeError, ValueError):
                response["result"] = repr(value)
        return response
        

    @staticmethod
    def get_selected_ifc_entities():
        """
        Get the IFC entities corresponding to the currently selected Blender objects.
        
        Returns:
            List of IFC entities for the selected objects
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # Get currently selected objects
            selected_objects = bpy.context.selected_objects
            if not selected_objects:
                return {"selected_count": 0, "message": "No objects selected in Blender"}
            
            # Collect IFC entities from selected objects
            selected_entities = []
            for obj in selected_objects:
                if hasattr(obj, "BIMObjectProperties") and obj.BIMObjectProperties.ifc_definition_id:
                    entity_id = obj.BIMObjectProperties.ifc_definition_id
                    entity = file.by_id(entity_id)
                    if entity:
                        entity_info = {
                            "id": entity.GlobalId if hasattr(entity, "GlobalId") else f"Entity_{entity.id()}",
                            "ifc_id": entity.id(),
                            "type": entity.is_a(),
                            "name": entity.Name if hasattr(entity, "Name") else None,
                            "blender_name": obj.name
                        }
                        selected_entities.append(entity_info)
            
            return {
                "selected_count": len(selected_entities),
                "selected_entities": selected_entities
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}
        
    ### SPECIFIC IFC METHODS ###
        
    @staticmethod
    def get_ifc_project_info():
        """
        Get basic information about the IFC project.
        
        Returns:
            Dictionary with project name, description, and basic metrics
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # Get project information
            projects = file.by_type("IfcProject")
            if not projects:
                return {"error": "No IfcProject found in the model"}
            
            project = projects[0]
            
            # Basic project info
            info = {
                "id": project.GlobalId,
                "name": project.Name if hasattr(project, "Name") else "Unnamed Project",
                "description": project.Description if hasattr(project, "Description") else None,
                "entity_counts": {}
            }
            
            # Count entities by type
            entity_types = ["IfcWall", "IfcDoor", "IfcWindow", "IfcSlab", "IfcBeam", "IfcColumn", "IfcSpace", "IfcBuildingStorey"]
            for entity_type in entity_types:
                entities = file.by_type(entity_type)
                info["entity_counts"][entity_type] = len(entities)
            
            return info
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def list_ifc_entities(entity_type=None, limit=50, selected_only=False):
        """
        List IFC entities of a specific type.
        
        Parameters:
            entity_type: Type of IFC entity to list (e.g., "IfcWall")
            limit: Maximum number of entities to return
        
        Returns:
            List of entities with basic properties
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # If we're only looking at selected objects
            if selected_only:
                selected_result = BlenderMCPServer.get_selected_ifc_entities()
                
                # Check for errors
                if "error" in selected_result:
                    return selected_result
                    
                # If no objects are selected, return early
                if selected_result["selected_count"] == 0:
                    return selected_result
                    
                # If entity_type is specified, filter the selected entities
                if entity_type:
                    filtered_entities = [
                        entity for entity in selected_result["selected_entities"]
                        if entity["type"] == entity_type
                    ]
                    
                    return {
                        "type": entity_type,
                        "selected_count": len(filtered_entities),
                        "entities": filtered_entities[:limit]
                    }
                else:
                    # Group selected entities by type
                    entity_types = {}
                    for entity in selected_result["selected_entities"]:
                        entity_type = entity["type"]
                        if entity_type in entity_types:
                            entity_types[entity_type].append(entity)
                        else:
                            entity_types[entity_type] = [entity]
                    
                    return {
                        "selected_count": selected_result["selected_count"],
                        "entity_types": [
                            {"type": t, "count": len(entities), "entities": entities[:limit]}
                            for t, entities in entity_types.items()
                        ]
                    }
            
            # Original functionality for non-selected mode
            if not entity_type:
                # If no type specified, list available entity types
                entity_types = {}
                for entity in file:
                    entity_type = entity.is_a()
                    if entity_type in entity_types:
                        entity_types[entity_type] += 1
                    else:
                        entity_types[entity_type] = 1

                return {
                    "available_types": [{"type": k, "count": v} for k, v in entity_types.items()]
                }
            
            # Get entities of the specified type
            entities = file.by_type(entity_type)
            
            # Prepare the result
            result = {
                "type": entity_type,
                "total_count": len(entities),
                "entities": []
            }
            
            # Add entity data (limited)
            for i, entity in enumerate(entities):
                if i >= limit:
                    break
                    
                entity_data = {
                    "id": entity.GlobalId if hasattr(entity, "GlobalId") else f"Entity_{entity.id()}",
                    "name": entity.Name if hasattr(entity, "Name") else None
                }
                
                result["entities"].append(entity_data)
            
            return result
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def get_ifc_properties(global_id=None, selected_only=False):
        """
        Get all properties of a specific IFC entity.
        
        Parameters:
            global_id: GlobalId of the IFC entity
        
        Returns:
            Dictionary with entity information and properties
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # If we're only looking at selected objects
            if selected_only:
                selected_result = BlenderMCPServer.get_selected_ifc_entities()
                
                # Check for errors
                if "error" in selected_result:
                    return selected_result
                
                # If no objects are selected, return early
                if selected_result["selected_count"] == 0:
                    return selected_result
                
                # Process each selected entity
                result = {
                    "selected_count": selected_result["selected_count"],
                    "entities": []
                }
                
                for entity_info in selected_result["selected_entities"]:
                    # Find entity by GlobalId
                    entity = file.by_guid(entity_info["id"])
                    if not entity:
                        continue
                    
                    # Get basic entity info
                    entity_data = {
                        "id": entity.GlobalId,
                        "type": entity.is_a(),
                        "name": entity.Name if hasattr(entity, "Name") else None,
                        "description": entity.Description if hasattr(entity, "Description") else None,
                        "blender_name": entity_info["blender_name"],
                        "property_sets": {}
                    }
                    
                    # Get all property sets
                    psets = ifcopenshell.util.element.get_psets(entity)
                    for pset_name, pset_data in psets.items():
                        entity_data["property_sets"][pset_name] = pset_data
                    
                    result["entities"].append(entity_data)
                
                return result
                
            # If we're looking at a specific entity
            elif global_id:
                # Find entity by GlobalId
                entity = file.by_guid(global_id)
                if not entity:
                    return {"error": f"No entity found with GlobalId: {global_id}"}
                
                # Get basic entity info
                entity_info = {
                    "id": entity.GlobalId,
                    "type": entity.is_a(),
                    "name": entity.Name if hasattr(entity, "Name") else None,
                    "description": entity.Description if hasattr(entity, "Description") else None,
                    "property_sets": {}
                }
                
                # Get all property sets
                psets = ifcopenshell.util.element.get_psets(entity)
                for pset_name, pset_data in psets.items():
                    entity_info["property_sets"][pset_name] = pset_data
                
                return entity_info
            else:
                return {"error": "Either global_id or selected_only must be specified"}
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def get_ifc_spatial_structure():
        """
        Get the spatial structure of the IFC model (site, building, storey, space hierarchy).
        
        Returns:
            Hierarchical structure of the IFC model's spatial elements
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # Start with projects
            projects = file.by_type("IfcProject")
            if not projects:
                return {"error": "No IfcProject found in the model"}
            
            def get_children(parent):
                """Get immediate children of the given element"""
                if hasattr(parent, "IsDecomposedBy"):
                    rel_aggregates = parent.IsDecomposedBy
                    children = []
                    for rel in rel_aggregates:
                        children.extend(rel.RelatedObjects)
                    return children
                return []
                
            def create_structure(element):
                """Recursively create the structure for an element"""
                result = {
                    "id": element.GlobalId,
                    "type": element.is_a(),
                    "name": element.Name if hasattr(element, "Name") else None,
                    "children": []
                }
                
                for child in get_children(element):
                    result["children"].append(create_structure(child))
                
                return result
            
            # Create the structure starting from the project
            structure = create_structure(projects[0])
            
            return structure
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def get_ifc_total_structure():
        """
        Get the complete IFC structure including spatial hierarchy and building elements.
        This function extends the spatial structure to include building elements like walls,
        doors, windows, etc. that are contained in each spatial element.

        Returns:
            Complete hierarchical structure with spatial elements and their contained building elements
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}

            # Start with projects
            projects = file.by_type("IfcProject")
            if not projects:
                return {"error": "No IfcProject found in the model"}

            def get_spatial_children(parent):
                """Get immediate spatial children of the given element"""
                if hasattr(parent, "IsDecomposedBy"):
                    rel_aggregates = parent.IsDecomposedBy
                    children = []
                    for rel in rel_aggregates:
                        children.extend(rel.RelatedObjects)
                    return children
                return []

            def get_contained_elements(spatial_element):
                """Get building elements contained in this spatial element"""
                contained_elements = []

                # Check for IfcRelContainedInSpatialStructure relationships
                if hasattr(spatial_element, "ContainsElements"):
                    for rel in spatial_element.ContainsElements:
                        for element in rel.RelatedElements:
                            element_info = {
                                "id": element.GlobalId,
                                "type": element.is_a(),
                                "name": element.Name if hasattr(element, "Name") else None,
                                "description": element.Description if hasattr(element, "Description") else None
                            }
                            contained_elements.append(element_info)

                return contained_elements

            def create_total_structure(element):
                """Recursively create the complete structure for an element"""
                result = {
                    "id": element.GlobalId,
                    "type": element.is_a(),
                    "name": element.Name if hasattr(element, "Name") else None,
                    "description": element.Description if hasattr(element, "Description") else None,
                    "children": [],
                    "building_elements": []
                }

                # Add spatial children (other spatial elements)
                for child in get_spatial_children(element):
                    result["children"].append(create_total_structure(child))

                # Add contained building elements (walls, doors, windows, etc.)
                result["building_elements"] = get_contained_elements(element)

                return result

            # Create the complete structure starting from the project
            total_structure = create_total_structure(projects[0])

            return total_structure

        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def get_ifc_relationships(global_id):
        """
        Get all relationships for a specific IFC entity.
        
        Parameters:
            global_id: GlobalId of the IFC entity
        
        Returns:
            Dictionary with all relationships the entity participates in
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # Find entity by GlobalId
            entity = file.by_guid(global_id)
            if not entity:
                return {"error": f"No entity found with GlobalId: {global_id}"}
            
            # Basic entity info
            entity_info = {
                "id": entity.GlobalId,
                "type": entity.is_a(),
                "name": entity.Name if hasattr(entity, "Name") else None,
                "relationships": {
                    "contains": [],
                    "contained_in": [],
                    "connects": [],
                    "connected_by": [],
                    "defines": [],
                    "defined_by": []
                }
            }
            
            # Check if entity contains other elements
            if hasattr(entity, "IsDecomposedBy"):
                for rel in entity.IsDecomposedBy:
                    for obj in rel.RelatedObjects:
                        entity_info["relationships"]["contains"].append({
                            "id": obj.GlobalId,
                            "type": obj.is_a(),
                            "name": obj.Name if hasattr(obj, "Name") else None
                        })
            
            # Check if entity is contained in other elements
            if hasattr(entity, "Decomposes"):
                for rel in entity.Decomposes:
                    rel_obj = rel.RelatingObject
                    entity_info["relationships"]["contained_in"].append({
                        "id": rel_obj.GlobalId,
                        "type": rel_obj.is_a(),
                        "name": rel_obj.Name if hasattr(rel_obj, "Name") else None
                    })
            
            # For physical connections (depends on entity type)
            if hasattr(entity, "ConnectedTo"):
                for rel in entity.ConnectedTo:
                    for obj in rel.RelatedElement:
                        entity_info["relationships"]["connects"].append({
                            "id": obj.GlobalId,
                            "type": obj.is_a(),
                            "name": obj.Name if hasattr(obj, "Name") else None,
                            "connection_type": rel.ConnectionType if hasattr(rel, "ConnectionType") else None
                        })
            
            if hasattr(entity, "ConnectedFrom"):
                for rel in entity.ConnectedFrom:
                    obj = rel.RelatingElement
                    entity_info["relationships"]["connected_by"].append({
                        "id": obj.GlobalId,
                        "type": obj.is_a(),
                        "name": obj.Name if hasattr(obj, "Name") else None,
                        "connection_type": rel.ConnectionType if hasattr(rel, "ConnectionType") else None
                    })
            
            return entity_info
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}
        

    @staticmethod
    def export_ifc_data(entity_type=None, level_name=None, output_format="csv"):
        """Export IFC data to a structured file"""
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            data_list = []
            
            # Filter objects based on type
            if entity_type:
                objects = file.by_type(entity_type)
            else:
                objects = file.by_type("IfcElement")
            
            # Create a data dictionary for each object
            for obj in objects:
                obj_data = {}
                
                # Get level/storey information
                container_level = None
                try:
                    containing_structure = ifcopenshell.util.element.get_container(obj)
                    if containing_structure and containing_structure.is_a("IfcBuildingStorey"):
                        container_level = containing_structure.Name
                except Exception as e:
                    pass
                
                # Skip if we're filtering by level and this doesn't match
                if level_name and container_level != level_name:
                    continue
                    
                # Basic information
                obj_data['ExpressId'] = obj.id()
                obj_data['GlobalId'] = obj.GlobalId if hasattr(obj, "GlobalId") else None
                obj_data['IfcClass'] = obj.is_a()
                obj_data['Name'] = obj.Name if hasattr(obj, "Name") else None
                obj_data['Description'] = obj.Description if hasattr(obj, "Description") else None
                obj_data['LevelName'] = container_level
                
                # Get predefined type if available
                try:
                    obj_data['PredefinedType'] = ifcopenshell.util.element.get_predefined_type(obj)
                except:
                    obj_data['PredefinedType'] = None
                    
                # Get type information
                try:
                    type_obj = ifcopenshell.util.element.get_type(obj)
                    obj_data['TypeName'] = type_obj.Name if type_obj and hasattr(type_obj, "Name") else None
                    obj_data['TypeClass'] = type_obj.is_a() if type_obj else None
                except:
                    obj_data['TypeName'] = None
                    obj_data['TypeClass'] = None
                
                # Get property sets (simplify structure for export)
                try:
                    property_sets = ifcopenshell.util.element.get_psets(obj)
                    # Flatten property sets for better export compatibility
                    for pset_name, pset_data in property_sets.items():
                        for prop_name, prop_value in pset_data.items():
                            obj_data[f"{pset_name}.{prop_name}"] = prop_value
                except Exception as e:
                    pass
                    
                data_list.append(obj_data)
            
            if not data_list:
                return "No data found matching the specified criteria"
            
            # Determine output directory - try multiple options to ensure it works in various environments
            output_dirs = [
                "C:\\Users\\Public\\Documents" if os.name == "nt" else None,  # Public Documents
                "/usr/share" if os.name != "nt" else None,  # Unix share directory
                "/tmp",  # Unix temp directory
                "C:\\Temp" if os.name == "nt" else None,  # Windows temp directory
            ]
            
            output_dir = None
            for dir_path in output_dirs:
                if dir_path and os.path.exists(dir_path) and os.access(dir_path, os.W_OK):
                    output_dir = dir_path
                    break
                    
            if not output_dir:
                return {"error": "Could not find a writable directory for output"}
            
            # Create filename based on filters
            filters = []
            if entity_type:
                filters.append(entity_type)
            if level_name:
                filters.append(level_name)
            filter_str = "_".join(filters) if filters else "all"
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"ifc_export_{filter_str}_{timestamp}.{output_format}"
            filepath = os.path.join(output_dir, filename)
            
            # Export based on format
            if output_format == "json":
                with open(filepath, 'w') as f:
                    json.dump(data_list, f, indent=2)
            elif output_format == "csv":
                import pandas as pd
                df = pd.DataFrame(data_list)
                df.to_csv(filepath, index=False)
            
            # Summary info for the response
            entity_count = len(data_list)
            entity_types = set(item['IfcClass'] for item in data_list)
            levels = set(item['LevelName'] for item in data_list if item['LevelName'])
            
            return {
                "success": True,
                "message": f"Data exported successfully to {filepath}",
                "filepath": filepath,
                "format": output_format,
                "summary": {
                    "entity_count": entity_count,
                    "entity_types": list(entity_types),
                    "levels": list(levels)
                }
            }
        
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}
        
    
    @staticmethod
    def place_ifc_object(type_name, location, rotation=None):
        """
        Place an IFC object at specified location with optional rotation
        
        Args:
            type_name: Name of the IFC element type
            location: [x, y, z] list or tuple for position
            rotation: Value in degrees for rotation around Z axis (optional)
        
        Returns:
            Dictionary with information about the created object
        """
        try:
            import ifcopenshell
            from bonsai.bim.ifc import IfcStore
            import math
            
            # Convert location to tuple if it's not already
            if isinstance(location, list):
                location = tuple(location)
                
            def find_type_by_name(name):
                file = IfcStore.get_file()
                for element in file.by_type("IfcElementType"):
                    if element.Name == name:
                        return element.id()
                return None

            # Find the type ID
            type_id = find_type_by_name(type_name)
            if not type_id:
                return {"error": f"Type '{type_name}' not found. Please check if this type exists in the model."}
                
            # Store original context
            original_context = bpy.context.copy()
            
            # Ensure we're in 3D View context
            override = bpy.context.copy()
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    override["area"] = area
                    override["region"] = area.regions[-1]
                    break
            
            # Set cursor location
            bpy.context.scene.cursor.location = location
            
            # Get properties to set up parameters
            props = bpy.context.scene.BIMModelProperties
            
            # Store original rl_mode and set to CURSOR to use cursor's Z position
            original_rl_mode = props.rl_mode
            props.rl_mode = 'CURSOR'
            
            # Create the object using the override context
            with bpy.context.temp_override(**override):
                bpy.ops.bim.add_occurrence(relating_type_id=type_id)
            
            # Get the newly created object
            obj = bpy.context.active_object
            if not obj:
                props.rl_mode = original_rl_mode
                return {"error": "Failed to create object"}
            
            # Force the Z position explicitly
            obj.location.z = location[2]
            
            # Apply rotation if provided
            if rotation is not None:
                # Convert degrees to radians for Blender's rotation_euler
                full_rotation = (0, 0, math.radians(float(rotation)))
                obj.rotation_euler = full_rotation
            
            # Sync the changes back to IFC
            # Use the appropriate method depending on what's available
            if hasattr(bpy.ops.bim, "update_representation"):
                bpy.ops.bim.update_representation(obj=obj.name)
            
            # Restore original rl_mode
            props.rl_mode = original_rl_mode
            
            # Get the IFC entity for the new object
            entity_id = obj.BIMObjectProperties.ifc_definition_id
            if entity_id:
                file = IfcStore.get_file()
                entity = file.by_id(entity_id)
                global_id = entity.GlobalId if hasattr(entity, "GlobalId") else None
            else:
                global_id = None
            
            # Return information about the created object
            return {
                "success": True,
                "blender_name": obj.name,
                "global_id": global_id,
                "location": list(obj.location),
                "rotation": list(obj.rotation_euler),
                "type_name": type_name
            }
            
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}
    

    ### Ability to see
    @staticmethod
    def get_current_view():
        """Capture and return the current viewport as an image"""
        try:
            # Find a 3D View
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    break
            else:
                return {"error": "No 3D View available"}
            
            # Create temporary file to save the viewport screenshot
            temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            temp_path = temp_file.name
            temp_file.close()
            
            # Find appropriate region
            for region in area.regions:
                if region.type == 'WINDOW':
                    break
            else:
                return {"error": "No appropriate region found in 3D View"}
            
            # Use temp_override instead of the old override dictionary
            with bpy.context.temp_override(area=area, region=region):
                # Save screenshot
                bpy.ops.screen.screenshot(filepath=temp_path)
            
            # Read the image data and encode as base64
            with open(temp_path, 'rb') as f:
                image_data = f.read()
            
            # Clean up
            os.unlink(temp_path)
            
            # Return base64 encoded image
            return {
                "width": area.width,
                "height": area.height,
                "format": "png",
                "data": base64.b64encode(image_data).decode('utf-8')
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}


    @staticmethod
    def get_ifc_quantities(entity_type=None, selected_only=False):
        """
        Calculate and get quantities (m2, m3, etc.) for IFC elements.
        
        Parameters:
            entity_type: Type of IFC entity to get quantities for (e.g., "IfcWall", "IfcSlab")
            selected_only: If True, only get quantities for selected objects
        
        Returns:
            Dictionary with quantities for the specified elements
        """
        try:
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}

            # Check if BaseQuantities already exist to avoid re-calculating
            quantities_exist = False
            sample_elements = file.by_type("IfcElement")[:10] if file.by_type("IfcElement") else []

            for elem in sample_elements:
                psets = ifcopenshell.util.element.get_psets(elem)
                if any(qset in psets for qset in ["BaseQuantities", "Qto_WallBaseQuantities",
                                                   "Qto_SlabBaseQuantities", "Qto_BeamBaseQuantities"]):
                    quantities_exist = True
                    break

            # Only calculate quantities if they don't exist yet
            if not quantities_exist:
                try:
                    bpy.ops.bim.perform_quantity_take_off()
                except Exception as e:
                    return {"error": f"Failed to calculate quantities: {str(e)}"}

            elements_data = []
            
            # If we're only looking at selected objects
            if selected_only:
                selected_result = BlenderMCPServer.get_selected_ifc_entities()
                
                # Check for errors
                if "error" in selected_result:
                    return selected_result
                
                # If no objects are selected, return early
                if selected_result["selected_count"] == 0:
                    return selected_result
                
                # Process each selected entity
                for entity_info in selected_result["selected_entities"]:
                    # Find entity by GlobalId
                    entity = file.by_guid(entity_info["id"])
                    if not entity:
                        continue
                    
                    # Filter by type if specified
                    if entity_type and entity.is_a() != entity_type:
                        continue
                    
                    # Extract quantities
                    element_data = extract_quantities(entity, entity_info["blender_name"])
                    if element_data:
                        elements_data.append(element_data)
                        
            else:
                # Get entities based on type or default to common element types
                if entity_type:
                    entities = file.by_type(entity_type)
                else:
                    # Get common element types that have quantities
                    entity_types = ["IfcWall", "IfcSlab", "IfcBeam", "IfcColumn", "IfcDoor", "IfcWindow"]
                    entities = []
                    for etype in entity_types:
                        entities.extend(file.by_type(etype))
                
                # Process each entity
                for entity in entities:
                    element_data = extract_quantities(entity)
                    if element_data:
                        elements_data.append(element_data)
            
            # Summary statistics
            summary = {
                "total_elements": len(elements_data),
                "element_types": {}
            }
            
            # Group by element type for summary
            for element in elements_data:
                etype = element["type"]
                if etype not in summary["element_types"]:
                    summary["element_types"][etype] = {"count": 0, "total_area": 0, "total_volume": 0}
                
                summary["element_types"][etype]["count"] += 1
                if element["quantities"].get("area"):
                    summary["element_types"][etype]["total_area"] += element["quantities"]["area"]
                if element["quantities"].get("volume"):
                    summary["element_types"][etype]["total_volume"] += element["quantities"]["volume"]
            
            return {
                "success": True,
                "elements": elements_data,
                "summary": summary
            }
            
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}
    
    @staticmethod
    def export_drawing_png(view_type="top", height_offset=0.5, resolution_x=1920, 
                             resolution_y=1080, storey_name=None, output_path=None):
        """
        Export drawings as PNG images with custom resolution.
        
        Creates 2D and 3D views of IFC building, particularly useful for architectural drawings.
        
        Args:
            view_type: "top" for plan view, "front", "right", "left" for elevations, "isometric" for 3D view
            height_offset: Height in meters above storey level for camera position  
            resolution_x: Horizontal resolution in pixels
            resolution_y: Vertical resolution in pixels
            storey_name: Specific storey name to render (None for all/ground floor)
            output_path: File path to save PNG (None for temp file)
        
        Returns:
            Dict with base64 encoded image data and metadata
        """
        try:
            import tempfile
            import os
            
            # Validate parameters
            if resolution_x > 4096 or resolution_y > 4096:
                return {"error": "Resolution too high. Maximum: 4096x4096"}
            
            if resolution_x < 100 or resolution_y < 100:
                return {"error": "Resolution too low. Minimum: 100x100"}
            
            # Check if IFC file is loaded
            file = IfcStore.get_file()
            if file is None:
                return {"error": "No IFC file is currently loaded"}
            
            # Store original render settings
            scene = bpy.context.scene
            original_engine = scene.render.engine
            original_res_x = scene.render.resolution_x
            original_res_y = scene.render.resolution_y
            original_filepath = scene.render.filepath
            
            # Set up render settings for drawing
            scene.render.engine = 'BLENDER_WORKBENCH'  # Fast, good for architectural drawings
            scene.render.resolution_x = resolution_x
            scene.render.resolution_y = resolution_y
            scene.render.resolution_percentage = 100
            
            # Store original camera if exists
            original_camera = bpy.context.scene.camera
            
            # Create temporary camera for orthographic rendering
            bpy.ops.object.camera_add()
            camera = bpy.context.object
            camera.name = "TempDrawingCamera"
            bpy.context.scene.camera = camera
            
            # Set camera to orthographic
            camera.data.type = 'ORTHO'
            camera.data.ortho_scale = 50  # Adjust based on building size
            
            # Position camera based on view type and storey
            if view_type == "top":
                # Find building bounds to position camera appropriately
                all_objects = [obj for obj in bpy.context.scene.objects 
                              if obj.type == 'MESH' and obj.visible_get()]
                
                if all_objects:
                    # Calculate bounding box of all visible objects
                    min_x = min_y = min_z = float('inf')
                    max_x = max_y = max_z = float('-inf')
                    
                    for obj in all_objects:
                        bbox = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
                        for corner in bbox:
                            min_x = min(min_x, corner.x)
                            max_x = max(max_x, corner.x)
                            min_y = min(min_y, corner.y)  
                            max_y = max(max_y, corner.y)
                            min_z = min(min_z, corner.z)
                            max_z = max(max_z, corner.z)
                    
                    # Position camera above the building
                    center_x = (min_x + max_x) / 2
                    center_y = (min_y + max_y) / 2
                    
                    # For plan view, position camera above
                    camera_height = max_z + height_offset
                    camera.location = (center_x, center_y, camera_height)
                    camera.rotation_euler = (0, 0, 0)  # Look down
                    
                    # Adjust orthographic scale based on building size
                    building_width = max(max_x - min_x, max_y - min_y) * 1.2  # Add 20% margin
                    camera.data.ortho_scale = building_width
                else:
                    # Default position if no objects found
                    camera.location = (0, 0, 10)
                    camera.rotation_euler = (0, 0, 0)
            
            elif view_type in ["front", "right", "left"]:
                # For elevations, position camera accordingly
                # This is a simplified implementation - could be enhanced
                all_objects = [obj for obj in bpy.context.scene.objects 
                              if obj.type == 'MESH' and obj.visible_get()]
                
                if all_objects:
                    # Calculate bounds
                    min_x = min_y = min_z = float('inf')
                    max_x = max_y = max_z = float('-inf')
                    
                    for obj in all_objects:
                        bbox = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
                        for corner in bbox:
                            min_x = min(min_x, corner.x)
                            max_x = max(max_x, corner.x)
                            min_y = min(min_y, corner.y)
                            max_y = max(max_y, corner.y)
                            min_z = min(min_z, corner.z)
                            max_z = max(max_z, corner.z)
                    
                    center_x = (min_x + max_x) / 2
                    center_y = (min_y + max_y) / 2
                    center_z = (min_z + max_z) / 2
                    
                    building_depth = max(max_x - min_x, max_y - min_y) * 2
                    
                    if view_type == "front":
                        camera.location = (center_x, center_y - building_depth, center_z)
                        camera.rotation_euler = (1.5708, 0, 0)  # 90 degrees X rotation
                    elif view_type == "right":
                        camera.location = (center_x + building_depth, center_y, center_z)
                        camera.rotation_euler = (1.5708, 0, 1.5708)  # Look from right
                    elif view_type == "left":
                        camera.location = (center_x - building_depth, center_y, center_z)
                        camera.rotation_euler = (1.5708, 0, -1.5708)  # Look from left
                    
                    # Adjust scale for elevations
                    building_height = max_z - min_z
                    building_width = max(max_x - min_x, max_y - min_y)
                    camera.data.ortho_scale = max(building_height, building_width) * 1.2
            
            elif view_type == "isometric":
                # For isometric view, use perspective camera positioned diagonally
                camera.data.type = 'PERSP'
                camera.data.lens = 35  # 35mm lens for nice perspective
                
                all_objects = [obj for obj in bpy.context.scene.objects 
                              if obj.type == 'MESH' and obj.visible_get()]
                
                if all_objects:
                    # Calculate bounds
                    min_x = min_y = min_z = float('inf')
                    max_x = max_y = max_z = float('-inf')
                    
                    for obj in all_objects:
                        bbox = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
                        for corner in bbox:
                            min_x = min(min_x, corner.x)
                            max_x = max(max_x, corner.x)
                            min_y = min(min_y, corner.y)
                            max_y = max(max_y, corner.y)
                            min_z = min(min_z, corner.z)
                            max_z = max(max_z, corner.z)
                    
                    center_x = (min_x + max_x) / 2
                    center_y = (min_y + max_y) / 2
                    center_z = (min_z + max_z) / 2
                    
                    # Calculate distance to frame the building nicely
                    building_size = max(max_x - min_x, max_y - min_y, max_z - min_z)
                    distance = building_size * 1.2  # Distance multiplier for good framing
                    
                    # Position camera for isometric view (45° angles)
                    # Classic isometric position: up and back, looking down at 30°
                    import math
                    angle_rad = math.radians(45)
                    
                    camera_x = center_x + distance * math.cos(angle_rad)
                    camera_y = center_y - distance * math.sin(angle_rad)
                    camera_z = center_z + distance * 0.3  # Lower elevation for better facade view
                    
                    camera.location = (camera_x, camera_y, camera_z)
                    
                    # Point camera at building center
                    direction = mathutils.Vector((center_x - camera_x, center_y - camera_y, center_z - camera_z))
                    camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
                else:
                    # Default isometric position
                    camera.location = (15, -15, 10)
                    camera.rotation_euler = (1.1, 0, 0.785)  # ~63°, 0°, ~45°
            
            # Set up output file path
            if output_path:
                render_path = output_path
            else:
                temp_dir = tempfile.gettempdir()
                render_path = os.path.join(temp_dir, f"drawing_{view_type}_{int(time.time())}.png")
            
            scene.render.filepath = render_path
            scene.render.image_settings.file_format = 'PNG'
            
            # Render the image
            bpy.ops.render.render(write_still=True)
            
            # Read the rendered image and encode as base64
            if os.path.exists(render_path):
                with open(render_path, 'rb') as f:
                    image_data = f.read()
                
                # Clean up temporary file if we created it
                if not output_path:
                    os.remove(render_path)
                
                # Restore original settings
                scene.render.engine = original_engine
                scene.render.resolution_x = original_res_x
                scene.render.resolution_y = original_res_y
                scene.render.filepath = original_filepath
                bpy.context.scene.camera = original_camera
                
                # Delete temporary camera
                bpy.data.objects.remove(camera, do_unlink=True)
                
                # Return base64 encoded image
                import base64
                return {
                    "success": True,
                    "data": base64.b64encode(image_data).decode('utf-8'),
                    "format": "png",
                    "resolution": f"{resolution_x}x{resolution_y}",
                    "view_type": view_type,
                    "output_path": render_path if output_path else None
                }
            else:
                return {"error": "Failed to create render file"}
                
        except Exception as e:
            # Restore settings on error
            try:
                scene = bpy.context.scene
                scene.render.engine = original_engine
                scene.render.resolution_x = original_res_x
                scene.render.resolution_y = original_res_y 
                scene.render.filepath = original_filepath
                bpy.context.scene.camera = original_camera
                
                # Clean up camera if it exists
                if 'camera' in locals() and camera:
                    bpy.data.objects.remove(camera, do_unlink=True)
            except:
                pass
                
            import traceback
            return {"error": f"Error creating drawing: {str(e)}", 
                    "traceback": traceback.format_exc()}

    @staticmethod
    def get_ifc_georeferencing_info(include_contexts: bool = False):
        """
        Retrieves georeferencing information from the currently opened IFC file (CRS, MapConversion, WCS, TrueNorth, IfcSite).

        Args:
            include_contexts (bool): If True, adds the breakdown of RepresentationContexts and operations

        Returns:
            dict: Structure with:
            {
            "georeferenced": bool,
            "crs": {
                "name": str|None,
                "geodetic_datum": str|None,
                "vertical_datum": str|None,
                "map_unit": str|None
            },
            "map_conversion": {
                "eastings": float|None,
                "northings": float|None,
                "orthogonal_height": float|None,
                "scale": float|None,
                "x_axis_abscissa": float|None,
                "x_axis_ordinate": float|None
            },
            "world_coordinate_system": {"origin": [x,y,z]|None},
            "true_north": {"direction_ratios": [x,y]|None},
            "site": {
                "local_placement_origin": [x,y,z]|None,
                "ref_latitude": [deg,min,sec,millionth]|None,
                "ref_longitude": [deg,min,sec,millionth]|None,
                "ref_elevation": float|None
            },
            "contexts": [...],     # only if include_contexts=True
            "warnings": [...]
            }
        """
        try:
                        
            file = IfcStore.get_file()
            debug = {"entered": True, "has_ifc": file is not None, "projects": 0, "sites": 0, "contexts": 0}
            if file is None:
                return {"error": "No IFC file is currently loaded", "debug": debug}

            warnings = []
            result = {
                "georeferenced": False,
                "crs": {
                    "name": None,
                    "geodetic_datum": None,
                    "vertical_datum": None,
                    "map_unit": None
                },
                "map_conversion": {
                    "eastings": None,
                    "northings": None,
                    "orthogonal_height": None,
                    "scale": None,
                    "x_axis_abscissa": None,
                    "x_axis_ordinate": None
                },
                "world_coordinate_system": {"origin": None},
                "true_north": {"direction_ratios": None},
                "site": {
                    "local_placement_origin": None,
                    "ref_latitude": None,
                    "ref_longitude": None,
                    "ref_elevation": None
                },
                "contexts": [],
                "warnings": warnings,
                "debug":debug,
            }

            # --- IfcProject & RepresentationContexts ---
            projects = file.by_type("IfcProject")
            debug["projects"] = len(projects)
            if projects:
                project = projects[0]
                contexts = getattr(project, "RepresentationContexts", None) or []
                debug["contexts"] = len(contexts)
                for ctx in contexts:
                    ctx_entry = {
                        "context_identifier": getattr(ctx, "ContextIdentifier", None),
                        "context_type": getattr(ctx, "ContextType", None),
                        "world_origin": None,
                        "true_north": None,
                        "has_coordinate_operation": []
                    }

                    # WorldCoordinateSystem → Local origin
                    try:
                        wcs = getattr(ctx, "WorldCoordinateSystem", None)
                        if wcs and getattr(wcs, "Location", None):
                            loc = wcs.Location
                            if getattr(loc, "Coordinates", None):
                                coords = list(loc.Coordinates)
                                result["world_coordinate_system"]["origin"] = coords
                                ctx_entry["world_origin"] = coords
                    except Exception as e:
                        warnings.append(f"WorldCoordinateSystem read error: {str(e)}")

                    # TrueNorth
                    try:
                        if hasattr(ctx, "TrueNorth") and ctx.TrueNorth:
                            tn = ctx.TrueNorth
                            ratios = list(getattr(tn, "DirectionRatios", []) or [])
                            result["true_north"]["direction_ratios"] = ratios
                            ctx_entry["true_north"] = ratios
                    except Exception as e:
                        warnings.append(f"TrueNorth read error: {str(e)}")

                    # HasCoordinateOperation → IfcMapConversion / TargetCRS
                    try:
                        if hasattr(ctx, "HasCoordinateOperation") and ctx.HasCoordinateOperation:
                            for op in ctx.HasCoordinateOperation:
                                op_entry = {"type": op.is_a(), "target_crs": None, "map_conversion": None}

                                # TargetCRS
                                crs = getattr(op, "TargetCRS", None)
                                if crs:
                                    result["crs"]["name"] = getattr(crs, "Name", None)
                                    result["crs"]["geodetic_datum"] = getattr(crs, "GeodeticDatum", None)
                                    result["crs"]["vertical_datum"] = getattr(crs, "VerticalDatum", None)
                                    try:
                                        map_unit = getattr(crs, "MapUnit", None)
                                        result["crs"]["map_unit"] = map_unit.Name if map_unit else None
                                    except Exception:
                                        result["crs"]["map_unit"] = None

                                    op_entry["target_crs"] = {
                                        "name": result["crs"]["name"],
                                        "geodetic_datum": result["crs"]["geodetic_datum"],
                                        "vertical_datum": result["crs"]["vertical_datum"],
                                        "map_unit": result["crs"]["map_unit"]
                                    }

                                # IfcMapConversion
                                if op.is_a("IfcMapConversion"):
                                    mc = {
                                        "eastings": getattr(op, "Eastings", None),
                                        "northings": getattr(op, "Northings", None),
                                        "orthogonal_height": getattr(op, "OrthogonalHeight", None),
                                        "scale": getattr(op, "Scale", None),
                                        "x_axis_abscissa": getattr(op, "XAxisAbscissa", None),
                                        "x_axis_ordinate": getattr(op, "XAxisOrdinate", None)
                                    }
                                    result["map_conversion"].update(mc)
                                    op_entry["map_conversion"] = mc

                                ctx_entry["has_coordinate_operation"].append(op_entry)
                    except Exception as e:
                        warnings.append(f"HasCoordinateOperation read error: {str(e)}")

                    if include_contexts:
                        result["contexts"].append(ctx_entry)
            else:
                warnings.append("IfcProject entity was not found.")

            # --- IfcSite (lat/long/alt local origin of placement) ---
            try:
                sites = file.by_type("IfcSite")
                debug["sites"] = len(sites)
                if sites:
                    site = sites[0]
                    # LocalPlacement
                    try:
                        if getattr(site, "ObjectPlacement", None):
                            placement = site.ObjectPlacement
                            axisPlacement = getattr(placement, "RelativePlacement", None)
                            if axisPlacement and getattr(axisPlacement, "Location", None):
                                loc = axisPlacement.Location
                                if getattr(loc, "Coordinates", None):
                                    result["site"]["local_placement_origin"] = list(loc.Coordinates)
                    except Exception as e:
                        warnings.append(f"IfcSite.ObjectPlacement read error: {str(e)}")

                    # Lat/Long/Alt
                    try:
                        lat = getattr(site, "RefLatitude", None)
                        lon = getattr(site, "RefLongitude", None)
                        ele = getattr(site, "RefElevation", None)
                        result["site"]["ref_latitude"]  = list(lat) if lat else None
                        result["site"]["ref_longitude"] = list(lon) if lon else None
                        result["site"]["ref_elevation"] = ele
                    except Exception as e:
                        warnings.append(f"IfcSite (lat/long/elev) read error: {str(e)}")
                else:
                    warnings.append("IfcSite was not found.")
            except Exception as e:
                warnings.append(f"Error while querying IfcSite: {str(e)}")

            # --- Heuristic to determine georeferencing ---
            geo_flags = [
                any(result["crs"].values()),
                any(v is not None for v in result["map_conversion"].values())          
            ]
            result["georeferenced"] = all(geo_flags)

            return result

        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()} 
    
    @staticmethod
    def georeference_ifc_model(
        crs_mode: str,
        epsg: int = None,
        crs_name: str = None,
        geodetic_datum: str = None,
        map_projection: str = None,
        map_zone: str = None,
        eastings: float = None,
        northings: float = None,
        orthogonal_height: float = 0.0,
        scale: float = 1.0,
        x_axis_abscissa: float = None,
        x_axis_ordinate: float = None,
        true_north_azimuth_deg: float = None,
        context_filter: str = "Model",
        context_index: int = None,
        site_ref_latitude: list = None,         # IFC format [deg, min, sec, millionth]
        site_ref_longitude: list = None,        # IFC format [deg, min, sec, millionth]
        site_ref_elevation: float = None,
        site_ref_latitude_dd: float = None,     # Decimal degrees (optional)
        site_ref_longitude_dd: float = None,    # Decimal degrees (optional)
        overwrite: bool = False,
        dry_run: bool = False,
        write_path: str = None,
    ):
        """
        Usage:
        Creates/updates IfcProjectedCRS + IfcMapConversion in the opened IFC.
        Optionally updates IfcSite.RefLatitude/RefLongitude/RefElevation.
        If `pyproj` is available, it can convert Lat/Long (degrees) ⇄ E/N (meters)
        according to the given EPSG.

        Requirements:
        CRS declaration is ALWAYS required:
        - crs_mode="epsg" + epsg=XXXX    OR
        - crs_mode="custom" + (crs_name, geodetic_datum, map_projection [, map_zone])

        Minimum MapConversion information:
        - eastings + northings
        (if missing but lat/long + EPSG + pyproj are available, they are computed)
        """
        import math
        from bonsai.bim.ifc import IfcStore
        file = IfcStore.get_file()
        if file is None:
            return {"success": False, "error": "No IFC file is currently loaded"}

        warnings = []
        actions = {"created_crs": False, "created_map_conversion": False,
                "updated_map_conversion": False, "updated_site": False,
                "overwrote": False, "wrote_file": False}
        debug = {}

        # ---------- helpers ----------
        def dd_to_ifc_dms(dd: float):
            """Converts decimal degrees to [deg, min, sec, millionth] (sign carried by degrees)."""
            if dd is None:
                return None
            sign = -1 if dd < 0 else 1
            v = abs(dd)
            deg = int(v)
            rem = (v - deg) * 60
            minutes = int(rem)
            sec_float = (rem - minutes) * 60
            seconds = int(sec_float)
            millionth = int(round((sec_float - seconds) * 1_000_000))
            # Normalizes rounding (e.g. 59.999999 → 60)
            if millionth == 1_000_000:
                seconds += 1
                millionth = 0
            if seconds == 60:
                minutes += 1
                seconds = 0
            if minutes == 60:
                deg += 1
                minutes = 0
            return [sign * deg, minutes, seconds, millionth]

        def select_context():
            ctxs = file.by_type("IfcGeometricRepresentationContext") or []
            if not ctxs:
                return None, "No IfcGeometricRepresentationContext found"
            if context_index is not None and 0 <= context_index < len(ctxs):
                return ctxs[context_index], None
            # By filter (default "Model", case-insensitive)
            if context_filter:
                for c in ctxs:
                    if (getattr(c, "ContextType", None) or "").lower() == context_filter.lower():
                        return c, None
            # Fallback to the first one
            return ctxs[0], None

        # ---------- 1) CRS Validation ----------
        if crs_mode not in ("epsg", "custom"):
            return {"success": False, "error": "crs_mode must be 'epsg' or 'custom'"}

        if crs_mode == "epsg":
            if not epsg:
                return {"success": False, "error": "epsg code required when crs_mode='epsg'"}
            crs_name_final = f"EPSG:{epsg}"
            geodetic_datum = geodetic_datum or "WGS84"
            map_projection = map_projection or "TransverseMercator"  # usual UTM
            # map_zone is optional
        else:
            # custom
            missing = [k for k in ("crs_name", "geodetic_datum", "map_projection") if locals().get(k) in (None, "")]
            if missing:
                return {"success": False, "error": f"Missing fields for custom CRS: {', '.join(missing)}"}
            crs_name_final = crs_name

        # ---------- 2) Complete E/N from Lat/Long (if missing and pyproj is available) ----------
        proj_used = None
        try:
            if (eastings is None or northings is None) and (site_ref_latitude_dd is not None and site_ref_longitude_dd is not None) and crs_mode == "epsg":
                try:
                    from pyproj import Transformer
                    # Assume lat/long in WGS84; if the EPSG is not WGS84-derived, pyproj handles the conversion
                    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
                    e, n = transformer.transform(site_ref_longitude_dd, site_ref_latitude_dd)
                    eastings = e if eastings is None else eastings
                    northings = n if northings is None else northings
                    proj_used = f"EPSG:4326->EPSG:{epsg}"
                except Exception as _e:
                    warnings.append(f"Could not convert Lat/Long to E/N: {_e}. Provide eastings/northings manually.")
        except Exception as _e:
            warnings.append(f"pyproj not available to compute E/N: {_e}. Provide eastings/northings manually.")

        # ---------- E/N Validation ----------
        if eastings is None or northings is None:
            return {"success": False, "error": "eastings and northings are required (or provide lat/long + EPSG with pyproj installed)"}

        # ---------- 3) Select context ----------
        context, ctx_err = select_context()
        if not context:
            return {"success": False, "error": ctx_err or "No context found"}

        # ---------- 4) Detect existing ones and handle overwrite ----------
        # Inverse: context.HasCoordinateOperation is already handled by ifcopenshell as an attribute
        existing_ops = list(getattr(context, "HasCoordinateOperation", []) or [])
        existing_map = None
        existing_crs = None
        for op in existing_ops:
            if op.is_a("IfcMapConversion"):
                existing_map = op
                existing_crs = getattr(op, "TargetCRS", None)
                break

        if existing_map and not overwrite:
            return {
                "success": True,
                "georeferenced": True,
                "message": "MapConversion already exists. Use overwrite=True to replace it.",
                "context_used": {"identifier": getattr(context, "ContextIdentifier", None), "type": getattr(context, "ContextType", None)},
                "map_conversion": {
                    "eastings": getattr(existing_map, "Eastings", None),
                    "northings": getattr(existing_map, "Northings", None),
                    "orthogonal_height": getattr(existing_map, "OrthogonalHeight", None),
                    "scale": getattr(existing_map, "Scale", None),
                    "x_axis_abscissa": getattr(existing_map, "XAxisAbscissa", None),
                    "x_axis_ordinate": getattr(existing_map, "XAxisOrdinate", None),
                },
                "crs": {
                    "name": getattr(existing_crs, "Name", None) if existing_crs else None,
                    "geodetic_datum": getattr(existing_crs, "GeodeticDatum", None) if existing_crs else None,
                    "map_projection": getattr(existing_crs, "MapProjection", None) if existing_crs else None,
                    "map_zone": getattr(existing_crs, "MapZone", None) if existing_crs else None,
                },
                "warnings": warnings,
                "actions": actions,
            }

        # ---------- 5) Build/Update CRS ----------
        if existing_crs and overwrite:
            actions["overwrote"] = True
            try:
                file.remove(existing_crs)
            except Exception:
                warnings.append("Could not remove the existing CRS; a new one will be created anyway.")

        # If custom, use the provided values; if EPSG, build the name and defaults
        crs_kwargs = {
            "Name": crs_name_final,
            "GeodeticDatum": geodetic_datum,
            "MapProjection": map_projection,
        }
        if map_zone:
            crs_kwargs["MapZone"] = map_zone

        crs_entity = file.create_entity("IfcProjectedCRS", **crs_kwargs)
        actions["created_crs"] = True

        # ---------- 6) Calculate orientation (optional) ----------
        # If true_north_azimuth_deg is given as the azimuth from North (model +Y axis) towards East (clockwise),
        # We can derive an approximate X vector: X = (cos(az+90°), sin(az+90°)).
        if (x_axis_abscissa is None or x_axis_ordinate is None) and (true_north_azimuth_deg is not None):
            az = math.radians(true_north_azimuth_deg)
            # Estimated X vector rotated 90° from North:
            x_axis_abscissa = math.cos(az + math.pi / 2.0)
            x_axis_ordinate = math.sin(az + math.pi / 2.0)

        # Defaults if still missing
        x_axis_abscissa = 1.0 if x_axis_abscissa is None else float(x_axis_abscissa)
        x_axis_ordinate = 0.0 if x_axis_ordinate is None else float(x_axis_ordinate)
        scale = 1.0 if scale is None else float(scale)
        orthogonal_height = 0.0 if orthogonal_height is None else float(orthogonal_height)

        # ---------- 7) Build/Update IfcMapConversion ----------
        if existing_map and overwrite:
            try:
                file.remove(existing_map)
            except Exception:
                warnings.append("Could not remove the existing MapConversion; another one will be created anyway.")

        map_kwargs = {
            "SourceCRS": context,
            "TargetCRS": crs_entity,
            "Eastings": float(eastings),
            "Northings": float(northings),
            "OrthogonalHeight": float(orthogonal_height),
            "XAxisAbscissa": float(x_axis_abscissa),
            "XAxisOrdinate": float(x_axis_ordinate),
            "Scale": float(scale),
        }
        map_entity = file.create_entity("IfcMapConversion", **map_kwargs)
        actions["created_map_conversion"] = True

        # ---------- 8) (Optional) Update IfcSite ----------
        try:
            sites = file.by_type("IfcSite") or []
            if sites:
                site = sites[0]
                # If no IFC lists are provided but decimal degrees are, convert them
                if site_ref_latitude is None and site_ref_latitude_dd is not None:
                    site_ref_latitude = dd_to_ifc_dms(site_ref_latitude_dd)
                if site_ref_longitude is None and site_ref_longitude_dd is not None:
                    site_ref_longitude = dd_to_ifc_dms(site_ref_longitude_dd)

                changed = False
                if site_ref_latitude is not None:
                    site.RefLatitude = site_ref_latitude
                    changed = True
                if site_ref_longitude is not None:
                    site.RefLongitude = site_ref_longitude
                    changed = True
                if site_ref_elevation is not None:
                    site.RefElevation = float(site_ref_elevation)
                    changed = True
                if changed:
                    actions["updated_site"] = True
            else:
                warnings.append("No IfcSite found; lat/long/elevation were not updated.")
        except Exception as e:
            warnings.append(f"Could not update IfcSite: {e}")

        # ---------- 9) (Optional) Save ----------
        if write_path and not dry_run:
            try:
                file.write(write_path)
                actions["wrote_file"] = True
            except Exception as e:
                warnings.append(f"Could not write IFC to'{write_path}': {e}")

        # ---------- 10) Response ----------
        return {
            "success": True,
            "georeferenced": True,
            "crs": {
                "name": getattr(crs_entity, "Name", None),
                "geodetic_datum": getattr(crs_entity, "GeodeticDatum", None),
                "map_projection": getattr(crs_entity, "MapProjection", None),
                "map_zone": getattr(crs_entity, "MapZone", None),
            },
            "map_conversion": {
                "eastings": float(eastings),
                "northings": float(northings),
                "orthogonal_height": float(orthogonal_height),
                "scale": float(scale),
                "x_axis_abscissa": float(x_axis_abscissa),
                "x_axis_ordinate": float(x_axis_ordinate),
            },
            "context_used": {
                "identifier": getattr(context, "ContextIdentifier", None),
                "type": getattr(context, "ContextType", None),
            },
            "site": {
                "ref_latitude": site_ref_latitude,
                "ref_longitude": site_ref_longitude,
                "ref_elevation": site_ref_elevation,
            },
            "proj_used": proj_used,
            "warnings": warnings,
            "actions": actions,
        }
    
    @staticmethod
    def generate_ids(
        title: str,
        specs: list,
        description: str = "",
        author: str = "",
        ids_version: str = "",
        purpose: str = "",
        milestone: str = "",
        output_path: str = None,
        date_iso: str = None,
    ):
        """
        Generates an .ids file with robust handling of:
            - Synonyms: 'name' → 'baseName', 'minValue/maxValue' + inclusivity, 'minOccurs/maxOccurs' → cardinality.
            - Operators inside 'value' ("> 30", "≤0.45"), in keys (op/target/threshold/limit), and extracted from 'description'
            (ONLY within requirements; never in applicability).
            - Correct restriction mapping:
                * Numeric → ids.Restriction(base="double" | "integer", options={...})
                * Textual (IFCLABEL/TEXT) → ids.Restriction(base="string", options={"pattern": [anchored regexes]})
            - Automatic dataType inference with hints 
            (ThermalTransmittance → IFCTHERMALTRANSMITTANCEMEASURE, IsExternal → IFCBOOLEAN, etc.).
            - PredefinedType remains as an Attribute within APPLICABILITY 
            (NOT absorbed into Entity.predefinedType).
        """
        
        #Libraries/Dependencies
        # -----------------------------------------------------------------------------------------------------------
        try:
            from ifctester import ids
        except Exception as e:
            return {"ok": False, "error": "Could not import ifctester.ids", "details": str(e)}

        import os, datetime, re
        from numbers import Number

        #Validations
        # -----------------------------------------------------------------------------------------------------------    
        if not isinstance(title, str) or not title.strip():
            return {"ok": False, "error": "Invalid or empty 'title' parameter."}
        if not isinstance(specs, list) or len(specs) == 0:
            return {"ok": False, "error": "You must provide at least one specification in 'specs'."}

        # Utils
        # -----------------------------------------------------------------------------------------------------------
        def _norm_card(c):
            """
            Usage:
                Normalizes the given cardinality value, ensuring it matches one of the valid terms.
            Inputs:
                c (str | None): Cardinality value to normalize. Can be 'required', 'optional', or 'prohibited'.
            Output:
                str | None: Normalized lowercase value if valid, or None if not provided.
            Exceptions:
                ValueError: Raised if the input value does not correspond to a valid cardinality.
            """
            if c is None: return None
            c = str(c).strip().lower()
            if c in ("required", "optional", "prohibited"): return c
            raise ValueError("Invalid cardinality: use 'required', 'optional', or 'prohibited'.")

        def _card_from_occurs(minOccurs, maxOccurs):
            """
            Usage:
                Derives the cardinality ('required' or 'optional') based on the values of minOccurs and maxOccurs.
            Inputs:
                minOccurs (int | str | None): Minimum number of occurrences. If greater than 0, the field is considered 'required'.
                maxOccurs (int | str | None): Maximum number of occurrences. Not used directly, included for completeness.
            Output:
                str | None: Returns 'required' if minOccurs > 0, 'optional' if minOccurs == 0, or None if conversion fails.
            """
            try:
                if minOccurs is None: return None
                m = int(minOccurs)
                return "required" if m > 0 else "optional"
            except Exception:
                return None

        def _is_bool_like(v):
            """
            Usage:
                Checks whether a given value can be interpreted as a boolean.
            Inputs:
                v (any): Value to evaluate. Can be of any type (bool, str, int, etc.).
            Output:
                bool: Returns True if the value represents a boolean-like token 
                    (e.g., True, False, "yes", "no", "1", "0", "y", "n", "t", "f"), 
                    otherwise returns False.
            """
            if isinstance(v, bool): return True
            if v is None: return False
            s = str(v).strip().lower()
            return s in ("true", "false", "1", "0", "yes", "no", "y", "n", "t", "f")

        def _to_bool_token(v):
            """
            Usage:
                Converts a boolean-like value into a standardized string token ("TRUE" or "FALSE").
            Inputs:
                v (any): Value to convert. Can be a boolean, string, or numeric value representing truthiness.
            Output:
                str | None: Returns "TRUE" or "FALSE" if the value matches a recognized boolean pattern,
                            or None if it cannot be interpreted as boolean.
            """        
            if isinstance(v, bool): return "TRUE" if v else "FALSE"
            s = str(v).strip().lower()
            if s in ("true", "1", "yes", "y", "t"): return "TRUE"
            if s in ("false", "0", "no", "n", "f"): return "FALSE"
            return None

        # Hints for *MEASURE* types and by property name
        MEASURE_HINTS = {
            "THERMALTRANSMITTANCE": "IFCTHERMALTRANSMITTANCEMEASURE",
            "UVALUE": "IFCTHERMALTRANSMITTANCEMEASURE",
            "RATIOMEASURE": "IFCRATIOMEASURE",
            "AREAMEASURE": "IFCAREAMEASURE",
            "LENGTHMEASURE": "IFCLENGTHMEASURE",
            "SOUNDPRESSURELEVELMEASURE": "IFCSOUNDPRESSURELEVELMEASURE",
        }
        PROPERTY_DATATYPE_HINTS = {
            "THERMALTRANSMITTANCE": "IFCTHERMALTRANSMITTANCEMEASURE",
            "ISEXTERNAL": "IFCBOOLEAN",
            "ACOUSTICRATING": "IFCLABEL",
        }

        def _norm_ifc_version(v: str | None) -> str | None:
            """
            Usage:
                Normalizes the given IFC schema version string to a standardized format.
            Inputs:
                v (str | None): Input version value (e.g., "4", "IFC 4", "2x3", "IFC4.3").
            Output:
                str | None: Returns the normalized IFC version (e.g., "IFC4", "IFC2X3", "IFC4X3"),
                            or None if the input is empty or invalid.
            """
            if not v: return None
            s = str(v).strip().upper()
            m = {"4": "IFC4", "IFC 4": "IFC4", "2X3": "IFC2X3", "IFC 2X3": "IFC2X3", "IFC4.3": "IFC4X3"}
            return m.get(s, s)

        def _strip_ifc_prefix(dt: str | None) -> str | None:
            """
            Usage:
                Removes leading and trailing spaces from the given string and converts it to uppercase.
                Typically used to normalize IFC data type names.
            Inputs:
                dt (str | None): Data type string to normalize (e.g., " ifcreal ").
            Output:
                str | None: Uppercase, trimmed string (e.g., "IFCREAL"), or None if the input is empty or None.
            """       
            return dt.strip().upper() if dt else None

        def _is_number_like(v) -> bool:
            """
            Usage:
                Checks whether the given value can be interpreted as a numeric value.
            Inputs:
                v (any): Value to evaluate. Can be of any type (int, float, str, etc.).
            Output:
                bool: Returns True if the value represents a number (including numeric strings like "3.5" or "2,7"),
                    otherwise returns False.
            """
            if isinstance(v, Number): return True
            if v is None: return False
            try:
                float(str(v).strip().replace(",", "."))
                return True
            except Exception:
                return False

        def _guess_numeric_base_from_ifc(dt_upper: str | None) -> str:
            """
            Usage:
                Determines the numeric base type ('integer' or 'double') from an IFC data type string.
            Inputs:
                dt_upper (str | None): Uppercase IFC data type name (e.g., "IFCINTEGER", "IFCREAL").
            Output:
                str: Returns "integer" if the type contains "INTEGER"; otherwise returns "double".
                    Defaults to "double" when no input is provided.
            """
            if not dt_upper: return "double"
            if "INTEGER" in dt_upper: return "integer"
            return "double"

        # comparators in string ("> 30", "<=0.45", "≥3", "≤ 3")
        _cmp_regex = re.compile(r"^\s*(>=|=>|≤|<=|≥|>|<)\s*([0-9]+(?:[.,][0-9]+)?)\s*$")
        _normalize_op = {">=":">=", "=>":">=", "≥":">=", "<=":"<=", "≤":"<="}
        
        def _extract_op_target_from_string(s: str):
            """
                Usage:
                    Extracts a comparison operator and its numeric target value from a string expression.
                Inputs:
                    s (str): String containing a comparison, e.g., "> 30", "<=0.45", "≥3", or "≤ 3".
                Output:
                    tuple(str | None, float | None): Returns a tuple (operator, target_value),
                                                    where operator is one of ">", ">=", "<", or "<=".
                                                    Returns (None, None) if the string does not match a valid pattern.
            """
            m = _cmp_regex.match(s)
            if not m: return None, None
            op, num = m.group(1), m.group(2)
            op = _normalize_op.get(op, op)
            try: tgt = float(num.replace(",", "."))
            except Exception: return None, None
            return op, tgt

        # English descriptions (>= before >)
        _desc_ops = [
            (r"(greater\s+than\s+or\s+equal\s+to|greater\s+or\s+equal\s+to|equal\s+or\s+greater\s+than|≥)", ">="),
            (r"(less\s+than\s+or\s+equal\s+to|not\s+greater\s+than|≤|at\s+most|maximum)", "<="),
            (r"(greater\s+than|more\s+than|>)", ">"),
            (r"(less\s+than|fewer\s+than|<)", "<"),
        ]
        _num_regex = re.compile(r"([0-9]+(?:[.,][0-9]+)?)")

        
        def _extract_from_description(desc: str):
            """
            Usage:
                Extracts a comparison operator and numeric target value from a descriptive text.
                Designed to interpret expressions such as "greater than 30" or "less than or equal to 0.45".
            Inputs:
                desc (str): Description text potentially containing a numeric comparison.
            Output:
                tuple(str | None, float | None): Returns a tuple (operator, target_value),
                                                where operator is one of ">", ">=", "<", or "<=",
                                                and target_value is the numeric value extracted.
                                                Returns (None, None) if no valid pattern is found.
            """
            if not desc: return None, None
            text = desc.strip().lower()
            for pat, op in _desc_ops:
                if re.search(pat, text):
                    m = _num_regex.search(text)
                    if m:
                        try:
                            tgt = float(m.group(1).replace(",", "."))
                            return op, tgt
                        except Exception:
                            pass
            return None, None

        # anchored regexes for integers (numeric fallback for decimals)
        def _regex_for_threshold(threshold: float, op: str) -> list[str]:
            """
                Usage:
                    Builds one or more anchored regular expressions to validate integer values 
                    against a numeric threshold and comparison operator.
                    For non-integer thresholds, returns a generic numeric pattern as fallback.
                Inputs:
                    threshold (float): Numeric limit used for the comparison (e.g., 30, 10.5).
                    op (str): Comparison operator, one of ">", ">=", "<", or "<=".
                Output:
                    list[str]: A list containing one or more anchored regex patterns that match 
                            integer strings satisfying the given condition.
                            Returns a generic numeric regex pattern as fallback for decimals.
            """
            if abs(threshold - round(threshold)) < 1e-9:
                t = int(round(threshold))
                def gt_int(n):
                    if n <= 8:  return rf"^([{n+1}-9]|[1-9]\d|[1-9]\d{{2,}})$"
                    if n <= 98:
                        tens, units = divmod(n + 1, 10)
                        p1 = rf"{tens}[{units}-9]" if units > 0 else rf"{tens}\d"
                        p2 = rf"[{tens+1}-9]\d" if tens < 9 else ""
                        parts = [p1, p2, r"[1-9]\d{2,}"]
                        return "^(" + "|".join([p for p in parts if p]) + ")$"
                    return r"^[1-9]\d{2,}$"
                def ge_int(n):
                    if n <= 9:  return rf"^([{n}-9]|[1-9]\d|[1-9]\d{{2,}})$"
                    if n <= 99:
                        tens, units = divmod(n, 10)
                        p1 = rf"{tens}[{units}-9]"
                        p2 = rf"[{tens+1}-9]\d" if tens < 9 else ""
                        parts = [p1, p2, r"[1-9]\d{2,}"]
                        return "^(" + "|".join([p for p in parts if p]) + ")$"
                    return r"^[1-9]\d{2,}$"
                def lt_int(n):
                    if n <= 0: return r"^(?!)$"
                    if n <= 10: return rf"^[0-9]$" if n == 10 else rf"^[0-{n-1}]$"
                    tens, units = divmod(n - 1, 10)
                    if tens == 1: return r"^([0-9]|1[0-9])$"
                    return rf"^([0-9]|[1-{tens-1}]\d|{tens}[0-{units}])$"
                def le_int(n):
                    if n < 10: return rf"^[0-{n}]$"
                    tens, units = divmod(n, 10)
                    if tens == 1:
                        return r"^([0-9]|1[0-9])$" if units == 9 else rf"^([0-9]|1[0-{units}])$"
                    parts = [r"[0-9]"]
                    if tens > 1: parts.append(rf"[1-{tens-1}]\d")
                    parts.append(rf"{tens}[0-{units}]")
                    return "^(" + "|".join(parts) + ")$"
                if   op == ">":  return [gt_int(t)]
                elif op == ">=": return [ge_int(t)]
                elif op == "<":  return [lt_int(t)]
                elif op == "<=": return [le_int(t)]
            return [r"^\d+(?:[.,]\d+)?$"]  # fallback for decimals (plain numeric string)

        def _build_restriction_for_text(op: str | None, target, bounds: dict):
            """
            Usage:
                Builds a text-based IDS restriction (ids.Restriction) using regex patterns derived 
                from numeric thresholds and comparison operators. 
                Used when a property has textual dataType (e.g., IFCLABEL) but represents numeric conditions.
            Inputs:
                op (str | None): Comparison operator (">", ">=", "<", "<=") if explicitly provided.
                target (any): Target value for the comparison. Can be numeric or string.
                bounds (dict): Dictionary of limit values such as 
                            {"minInclusive": ..., "maxExclusive": ..., "maxInclusive": ...}.
            Output:
                ids.Restriction | None: Returns an ids.Restriction object with regex patterns 
                                        for matching the specified numeric range in string form,
                                        or None if no valid pattern can be built.
            """
            if op and target is not None and _is_number_like(target):
                return ids.Restriction(base="string", options={"pattern": _regex_for_threshold(float(target), op)})
            patterns = []
            if bounds.get("minExclusive") is not None:
                patterns += _regex_for_threshold(float(bounds["minExclusive"]), ">")
            if bounds.get("minInclusive") is not None:
                patterns += _regex_for_threshold(float(bounds["minInclusive"]), ">=")
            if bounds.get("maxExclusive") is not None:
                patterns += _regex_for_threshold(float(bounds["maxExclusive"]), "<")
            if bounds.get("maxInclusive") is not None:
                patterns += _regex_for_threshold(float(bounds["maxInclusive"]), "<=")
            return ids.Restriction(base="string", options={"pattern": patterns}) if patterns else None

        def _build_numeric_restriction(dt_upper: str | None, op: str | None, target, bounds: dict):
            """
            Usage:
                Builds a numeric IDS restriction (ids.Restriction) from a data type, comparison operator, 
                target value, and optional numeric bounds.
            Inputs:
                dt_upper (str | None): Uppercase IFC data type name (e.g., "IFCREAL", "IFCINTEGER").
                op (str | None): Comparison operator (">", ">=", "<", "<=") if provided.
                target (any): Target value for the comparison. Converted to float when applicable.
                bounds (dict): Dictionary containing optional boundary values such as 
                            {"minInclusive": ..., "maxExclusive": ..., "maxInclusive": ...}.
            Output:
                ids.Restriction | None: Returns an ids.Restriction object with the appropriate numeric limits,
                                        or None if no valid restriction can be created.
            """
            if not (op or any(v is not None for v in bounds.values())): return None
            base_num = _guess_numeric_base_from_ifc(dt_upper)
            opts = {}
            if op and target is not None:
                v = float(str(target).replace(",", "."))
                if   op == ">":  opts["minExclusive"] = v
                elif op == ">=": opts["minInclusive"] = v
                elif op == "<":  opts["maxExclusive"] = v
                elif op == "<=": opts["maxInclusive"] = v
            for k in ("minInclusive","maxInclusive","minExclusive","maxExclusive"):
                if bounds.get(k) is not None:
                    opts[k] = float(str(bounds[k]).replace(",", "."))
            if not opts: return None
            return ids.Restriction(base=base_num, options=opts)

        def _infer_ids_datatype(pset: str | None, baseName: str | None,
                                provided_dt: str | None, value, op: str | None, bounds: dict) -> str:
            """
            Usage:
                Infers the appropriate IFC data type (e.g., IFCREAL, IFCINTEGER, IFCBOOLEAN, IFCLABEL)
                for a given property based on its name, provided data type, value, and restrictions.
            Inputs:
                pset (str | None): Name of the property set to which the property belongs.
                baseName (str | None): Base name of the property (e.g., "ThermalTransmittance", "IsExternal").
                provided_dt (str | None): Data type explicitly provided in the input, if any.
                value (any): Property value or an ids.Restriction object.
                op (str | None): Comparison operator (">", ">=", "<", "<=") if defined.
                bounds (dict): Dictionary containing limit values such as 
                            {"minInclusive": ..., "maxExclusive": ..., "maxInclusive": ...}.
            Output:
                str: Returns the inferred IFC data type string, such as "IFCREAL", "IFCINTEGER", 
                    "IFCBOOLEAN", or "IFCLABEL".
            """
            # if a dataType is provided, normalize and promote it if applicable
            if provided_dt:
                dtU = _strip_ifc_prefix(provided_dt)
                if baseName and dtU in ("IFCREAL", "IFCNUMBER", "NUMBER", "REAL"):
                    hint = PROPERTY_DATATYPE_HINTS.get(str(baseName).strip().upper())
                    if hint: return hint
                if dtU in MEASURE_HINTS: return MEASURE_HINTS[dtU]
                return dtU
            # hints by name
            if baseName:
                hint = PROPERTY_DATATYPE_HINTS.get(str(baseName).strip().upper())
                if hint: return hint
            # value = Restriction
            if isinstance(value, ids.Restriction):
                base = getattr(value, "base", "").lower()
                if base in ("integer",): return "IFCINTEGER"
                if base in ("double","number","real","float"): return "IFCREAL"
                return "IFCLABEL"
            # if op/bounds -> numeric
            if op or any(v is not None for v in bounds.values()):
                return "IFCREAL"
            # booleans
            if _is_bool_like(value): return "IFCBOOLEAN"
            # literal numbers
            if _is_number_like(value):
                try:
                    iv = int(str(value))
                    if float(str(value)) == float(iv): return "IFCINTEGER"
                except Exception:
                    pass
                return "IFCREAL"
            # text
            return "IFCLABEL"

        # (optional) Absorption of PredefinedType into Entity.predefinedType — DISABLED
        def _absorb_predefined_type(applicability_list: list):
            """
            Usage:
                Transfers the value of a PREDEFINEDTYPE attribute into the corresponding Entity's 
                predefinedType field within the applicability list. 
                This operation effectively absorbs the PREDEFINEDTYPE entry into the Entity definition.
            Inputs:
                applicability_list (list): List of facet dictionaries containing 'Entity' and 'Attribute' definitions.
            Output:
                list: The updated applicability list where the PREDEFINEDTYPE value has been moved 
                    to the Entity's 'predefinedType' field, if applicable. 
                    Returns the original list if no valid Entity or PREDEFINEDTYPE attribute is found.
            """
            if not isinstance(applicability_list, list): return applicability_list
            idx = next((i for i,f in enumerate(applicability_list) if (f.get("type") == "Entity")), None)
            if idx is None: return applicability_list
            for i,f in enumerate(list(applicability_list)):
                if f.get("type") == "Attribute" and str(f.get("name","")).strip().upper() == "PREDEFINEDTYPE":
                    val = f.get("value")
                    if val not in (None, ""):
                        applicability_list[idx]["predefinedType"] = val
                        applicability_list.pop(i)
                        break
            return applicability_list

        # IDS Root 
        # -----------------------------------------------------------------------------------------------------------
        try:
            ids_root = ids.Ids(
                title=(title or "Untitled"),
                description=(description or None),
                author=(author or None),
                version=(str(ids_version) if ids_version else None),
                purpose=(purpose or None),
                milestone=(milestone or None),
                date=(date_iso or datetime.date.today().isoformat()),
            )
            try: ids_root.title = (title or "Untitled")
            except Exception: pass
            try: ids_root.info.title = (title or "Untitled")
            except Exception: pass
        except Exception as e:
            return {"ok": False, "error": "Could not initialize the IDS", "details": str(e)}

        # Facets (with context)
        # -----------------------------------------------------------------------------------------------------------
        def _facet_from_dict(f, spec_desc: str | None, context: str):
            """
            Usage:
                Builds an IDS facet object (e.g., Entity, Attribute, Property, Material, Classification, or PartOf)
                from a dictionary definition. Handles data normalization, type inference, comparison extraction,
                and restriction creation for both applicability and requirements contexts.
            Inputs:
                f (dict): Dictionary describing a facet, including its type and relevant attributes.
                spec_desc (str | None): Optional specification description used to infer operators or targets
                                        when not explicitly provided.
                context (str): Indicates the facet context, either 'applicability' or 'requirements'.
                            Only in 'requirements' can operator/target be extracted from the description.
            Output:
                ids.Entity | ids.Attribute | ids.Property | ids.Material | ids.Classification | ids.PartOf:
                    Returns the corresponding ids.* object based on the facet type.
            Exceptions:
                ValueError: Raised if the facet type is unsupported or required fields are missing
                            (e.g., Property without propertySet or baseName, Attribute without name).
            """    
                        
            t = (f.get("type") or "").strip()

            if t == "Entity":
                ent_name = f.get("name", "") or f.get("entity", "") or f.get("Name", "")
                ent_name = ent_name.strip()
                if ent_name.lower().startswith("ifc") and not ent_name.isupper():
                    ent_name = ent_name.upper()  # 'IfcWall' -> 'IFCWALL'
                return ids.Entity(
                    name=ent_name,
                    predefinedType=f.get("predefinedType", ""),  # we keep it separate (not absorbed)
                    instructions=f.get("instructions", ""),
                )

            elif t == "Attribute":
                name = f.get("name") or f.get("Name")
                if not name: raise ValueError("Attribute requires 'name'.")
                kwargs = dict(name=name)
                if f.get("value") not in (None, ""):
                    val = f["value"]
                    if _is_bool_like(val):
                        tok = _to_bool_token(val)
                        kwargs["value"] = tok if tok else val
                    else:
                        kwargs["value"] = val
                # Cardinality from occurs
                card = _card_from_occurs(f.get("minOccurs"), f.get("maxOccurs"))
                if card: kwargs["cardinality"] = card
                if f.get("cardinality"): kwargs["cardinality"] = _norm_card(f.get("cardinality"))
                if f.get("instructions"): kwargs["instructions"] = f["instructions"]
                return ids.Attribute(**kwargs)

            elif t == "Property":
                pset = f.get("propertySet") or f.get("pset") or f.get("psetName")
                base = f.get("baseName") or f.get("name") or f.get("Name")
                if not pset or not base: raise ValueError("Property requires 'propertySet' and 'baseName'.")

                val_in = f.get("value", None)
                bounds = {
                    "minInclusive": f.get("minInclusive"),
                    "maxInclusive": f.get("maxInclusive"),
                    "minExclusive": f.get("minExclusive"),
                    "maxExclusive": f.get("maxExclusive"),
                }
                # minValue/maxValue + inclusivity
                if f.get("minValue") is not None:
                    if bool(f.get("minInclusive")): bounds["minInclusive"] = f.get("minValue")
                    else:                            bounds["minExclusive"] = f.get("minValue")
                if f.get("maxValue") is not None:
                    if bool(f.get("maxInclusive")): bounds["maxInclusive"] = f.get("maxValue")
                    else:                            bounds["maxExclusive"] = f.get("maxValue")

                if isinstance(val_in, dict):
                    for k in ("minInclusive","maxInclusive","minExclusive","maxExclusive"):
                        if k in val_in and bounds.get(k) is None:
                            bounds[k] = val_in[k]

                # explicit operator
                op = f.get("op") or f.get("operator") or f.get("comparison") or f.get("cmp") or f.get("relation")
                target = f.get("target") or f.get("threshold") or f.get("limit")

                # operator in 'value' string ("> 30")
                if target is None and isinstance(val_in, str):
                    _op2, _tg2 = _extract_op_target_from_string(val_in)
                    if _op2 and _tg2 is not None:
                        op, target, val_in = _op2, _tg2, None

                # ONLY IN REQUIREMENTS: extract from description
                if context == "requirements" and (not op and all(v is None for v in bounds.values()) and target is None and spec_desc):
                    _op3, _tg3 = _extract_from_description(spec_desc)
                    if _op3 and _tg3 is not None:
                        op, target = _op3, _tg3

                # cardinality from occurs
                card = _card_from_occurs(f.get("minOccurs"), f.get("maxOccurs"))

                dt = _infer_ids_datatype(pset, base, f.get("dataType"), val_in, op, bounds)

                # boolean normalization
                if _is_bool_like(val_in):
                    tok = _to_bool_token(val_in)
                    if tok is not None:
                        val_in = tok
                        if not dt: dt = "IFCBOOLEAN"

                # Restriction when applicable
                restriction_obj = None
                if op or any(v is not None for v in bounds.values()):
                    if dt in ("IFCLABEL","IFCTEXT"):
                        restriction_obj = _build_restriction_for_text(op, target if target is not None else val_in, bounds)
                    else:
                        restriction_obj = _build_numeric_restriction(dt, op, target if target is not None else val_in, bounds)
                if isinstance(val_in, ids.Restriction):
                    restriction_obj = val_in

                kwargs = dict(propertySet=pset, baseName=base)
                if restriction_obj is not None:
                    kwargs["value"] = restriction_obj
                    if dt: kwargs["dataType"] = dt
                else:
                    if val_in not in (None, ""): kwargs["value"] = val_in
                    if dt: kwargs["dataType"] = dt

                if f.get("uri"): kwargs["uri"] = f["uri"]
                if f.get("instructions"): kwargs["instructions"] = f["instructions"]
                if card: kwargs["cardinality"] = card
                if f.get("cardinality"): kwargs["cardinality"] = _norm_card(f.get("cardinality"))
                if (op or any(v is not None for v in bounds.values())) and "cardinality" not in kwargs:
                    kwargs["cardinality"] = "required"

                return ids.Property(**kwargs)

            elif t == "Material":
                kwargs = {}
                if f.get("value"): kwargs["value"] = f["value"]
                if f.get("uri"): kwargs["uri"] = f["uri"]
                if f.get("cardinality"): kwargs["cardinality"] = _norm_card(f["cardinality"])
                if f.get("instructions"): kwargs["instructions"] = f["instructions"]
                return ids.Material(**kwargs)

            elif t == "Classification":
                return ids.Classification(
                    value=f.get("value", ""),
                    system=f.get("system", ""),
                    uri=f.get("uri", ""),
                    cardinality=_norm_card(f.get("cardinality")),
                    instructions=f.get("instructions", ""),
                )

            elif t == "PartOf":
                return ids.PartOf(
                    name=f.get("name", ""),
                    predefinedType=f.get("predefinedType", ""),
                    relation=f.get("relation", ""),
                    cardinality=_norm_card(f.get("cardinality")),
                    instructions=f.get("instructions", ""),
                )

            else:
                raise ValueError(f"Unsupported or empty facet type: '{t}'.")

        # Construction
        # -----------------------------------------------------------------------------------------------------------
        total_specs = total_app = total_req = 0
        try:
            for s in specs:
                if not isinstance(s, dict):
                    raise ValueError("Each 'spec' must be a dict.")
                applicability = s.get("applicability", [])
                requirements  = s.get("requirements", [])
                if not isinstance(applicability, list) or not isinstance(requirements, list):
                    raise ValueError("'applicability' and 'requirements' must be lists.")

                # Do NOT absorb PredefinedType (it remains as an Attribute in applicability)
                # applicability = _absorb_predefined_type(applicability)

                spec_obj = ids.Specification()
                if s.get("name"):
                    try: spec_obj.name = s["name"]
                    except Exception: pass
                if s.get("description"):
                    try: spec_obj.description = s["description"]
                    except Exception: pass

                # ifcVersion: use the provided one; if not, default to IFC4
                canon = _norm_ifc_version(s.get("ifcVersion") or "IFC4")
                try: spec_obj.ifcVersion = canon
                except Exception: pass

                for f in applicability:
                    facet = _facet_from_dict(f, s.get("description"), context="applicability")
                    spec_obj.applicability.append(facet); total_app += 1

                for f in requirements:
                    facet = _facet_from_dict(f, s.get("description"), context="requirements")
                    spec_obj.requirements.append(facet); total_req += 1

                ids_root.specifications.append(spec_obj); total_specs += 1

        except Exception as e:
            return {"ok": False, "error": "Error while building the IDS specifications", "details": str(e)}

        if total_specs == 0:
            return {"ok": False, "error": "No Specification was created. Check 'specs'."}

        # Saved
        # -----------------------------------------------------------------------------------------------------------
        try:
            if not output_path:
                safe_title = "".join(c for c in title if c.isalnum() or c in (" ","-","_")).rstrip() or "ids"
                today = (date_iso if date_iso else datetime.date.today().isoformat())
                output_path = os.path.abspath(f"{safe_title}_{today}.ids")
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            ids_root.to_xml(output_path)
        except Exception as e:
            return {"ok": False, "error": "Could not save the IDS file", "details": str(e)}

        return {
            "ok": True,
            "output_path": output_path,
            "message": f"IDS '{title}' generated. Specs: {total_specs}, facets: {total_app} appl. / {total_req} req."
        }
    
    #endregion


def extract_quantities(entity, blender_name=None):
    """
    Extract quantity information from an IFC entity.
    
    Parameters:
        entity: IFC entity object
        blender_name: Optional Blender object name
    
    Returns:
        Dictionary with element info and quantities
    """
    try:
        # Get all property sets
        psets = ifcopenshell.util.element.get_psets(entity)
        
        # Basic element info
        element_data = {
            "id": entity.GlobalId if hasattr(entity, "GlobalId") else f"Entity_{entity.id()}",
            "name": entity.Name if hasattr(entity, "Name") else None,
            "type": entity.is_a(),
            "blender_name": blender_name,
            "quantities": {}
        }
        
        # Look for quantity information in different property sets
        quantity_sources = ["BaseQuantities", "ArchiCADQuantities", "Qto_WallBaseQuantities", 
                           "Qto_SlabBaseQuantities", "Qto_BeamBaseQuantities", "Qto_ColumnBaseQuantities"]
        
        # Extract quantities from property sets - keep original names
        for pset_name in quantity_sources:
            if pset_name in psets:
                pset_data = psets[pset_name]
                for prop_name, prop_value in pset_data.items():
                    # Only include numeric values and skip the 'id' field
                    if isinstance(prop_value, (int, float)) and prop_name != 'id':
                        element_data["quantities"][prop_name] = prop_value
            
        return element_data if element_data["quantities"] else None
        
    except Exception as e:
        return None


# Blender UI Panel
# =====================================================================
# GEOMETRY ANALYSIS & TEMPORARY VIEWPORT DRAWINGS
# ---------------------------------------------------------------------
# - Geometry is computed with ifcopenshell.geom (OpenCASCADE kernel) and
#   indexed in an ifcopenshell.geom.tree ("opencascade.trianglebvh"), the
#   same backend IfcClash uses. All coordinates are in metres (SI), in
#   the world coordinate system of the IFC model.
# - Temporary drawings (dimensions, bounding boxes, clash markers) are
#   GPU overlays in every 3D viewport. They are NOT saved in the .blend
#   or the .ifc and disappear on clear_temporary_drawings / addon reload.
# =====================================================================
import math
import multiprocessing

_GEOM_CACHE = {"file": None, "tree": None, "shapes": {}, "count": None}

_COLORS = {
    "orange": (1.0, 0.45, 0.0, 1.0),
    "red": (0.95, 0.1, 0.1, 1.0),
    "green": (0.1, 0.85, 0.2, 1.0),
    "blue": (0.15, 0.45, 1.0, 1.0),
    "yellow": (1.0, 0.9, 0.0, 1.0),
    "cyan": (0.0, 0.9, 0.95, 1.0),
    "magenta": (0.95, 0.2, 0.9, 1.0),
    "white": (1.0, 1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0, 1.0),
}
_AXIS_COLORS = ((0.95, 0.25, 0.25, 1.0), (0.3, 0.85, 0.3, 1.0), (0.3, 0.55, 1.0, 1.0))
_OVERLAY_KEY = "_bonsai_mcp_overlay_handles"
_OVERLAY = {"items": [], "next_id": 1}


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _r(v, nd=4):
    try:
        return [round(float(x), nd) for x in v]
    except TypeError:
        return round(float(v), nd)


def _parse_color(color, default="orange"):
    if color is None:
        color = default
    if isinstance(color, str):
        return _COLORS.get(color.lower(), _COLORS[default])
    c = [float(x) for x in color]
    if max(c) > 1.0:
        c = [x / 255.0 for x in c]
    if len(c) == 3:
        c.append(1.0)
    return tuple(c[:4])


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                return list(json.loads(value))
            except Exception:
                pass
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _element_info(entity):
    return {
        "global_id": getattr(entity, "GlobalId", None),
        "ifc_id": entity.id(),
        "type": entity.is_a(),
        "name": getattr(entity, "Name", None),
    }


def _resolve_elements(file, items=None, entity_type=None, selected_only=False):
    """Resolve a mix of GlobalIds, STEP ids and IFC class names into entities."""
    found = []
    for item in _as_list(items) + _as_list(entity_type):
        item = str(item)
        entity = None
        if item.lower().startswith("ifc"):
            try:
                found.extend(file.by_type(item))
                continue
            except Exception:
                pass
        try:
            entity = file.by_guid(item)
        except Exception:
            entity = None
        if entity is None and item.lstrip("#").isdigit():
            try:
                entity = file.by_id(int(item.lstrip("#")))
            except Exception:
                entity = None
        if entity is not None:
            found.append(entity)
    if selected_only:
        for obj in bpy.context.selected_objects:
            props = getattr(obj, "BIMObjectProperties", None)
            if props and props.ifc_definition_id:
                try:
                    found.append(file.by_id(props.ifc_definition_id))
                except Exception:
                    pass
    seen, result = set(), []
    for e in found:
        if e.id() not in seen:
            seen.add(e.id())
            result.append(e)
    return result


def _make_tree():
    try:
        return ifcopenshell.geom.tree(backend="opencascade.trianglebvh")
    except TypeError:  # IfcOpenShell < 0.9
        return ifcopenshell.geom.tree()


def _make_iterator(settings, file, elements):
    threads = max(1, multiprocessing.cpu_count() - 1)
    try:
        return ifcopenshell.geom.iterator(settings, file, threads, include=elements, geometry_library="opencascade")
    except TypeError:
        return ifcopenshell.geom.iterator(settings, file, threads, include=elements)


def _shape_summary(shape):
    import numpy as np
    import ifcopenshell.util.shape as ushape

    geometry = shape.geometry
    world = np.asarray(ushape.get_shape_vertices(shape, geometry), dtype=float)
    local = np.asarray(ushape.get_vertices(geometry), dtype=float)
    matrix = np.asarray(ushape.get_shape_matrix(shape), dtype=float)
    if not len(world):
        return None
    wmin, wmax = world.min(axis=0), world.max(axis=0)
    lmin, lmax = local.min(axis=0), local.max(axis=0)
    try:
        volume = float(ushape.get_volume(geometry))
    except Exception:
        volume = None
    try:
        area = float(ushape.get_area(geometry))
    except Exception:
        area = None
    bottom_area = top_area = None
    try:
        faces = np.asarray(geometry.faces, dtype=int).reshape(-1, 3)
        tri = world[faces]
        cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        tri_area = 0.5 * np.linalg.norm(cross, axis=1)
        nz = np.divide(cross[:, 2], 2 * tri_area, out=np.zeros_like(tri_area), where=tri_area > 0)
        bottom_area = float(tri_area[nz < -0.9].sum())
        top_area = float(tri_area[nz > 0.9].sum())
    except Exception:
        pass
    return {
        "bottom_area": bottom_area,
        "top_area": top_area,
        "aabb_min": wmin.tolist(),
        "aabb_max": wmax.tolist(),
        "local_min": lmin.tolist(),
        "local_max": lmax.tolist(),
        "matrix": matrix.tolist(),
        "volume": volume,
        "surface_area": area,
        "vertex_count": int(len(world)),
        "face_count": int(len(geometry.faces) // 3),
    }


def _get_geometry_cache(rebuild=False):
    file = IfcStore.get_file()
    if file is None:
        raise Exception("No IFC file is currently loaded")
    count = len(file.by_type("IfcProduct"))
    cache = _GEOM_CACHE
    if rebuild or cache["tree"] is None or cache["file"] is not file or cache["count"] != count:
        elements = [
            e for e in file.by_type("IfcElement")
            if not e.is_a("IfcFeatureElement") and getattr(e, "Representation", None)
        ]
        elements += [e for e in file.by_type("IfcSpace") if getattr(e, "Representation", None)]
        tree = _make_tree()
        shapes = {}
        meshes = {}
        if elements:
            iterator = _make_iterator(ifcopenshell.geom.settings(), file, elements)
            if iterator.initialize():
                while True:
                    shape = iterator.get()
                    try:
                        tree.add_element(shape)
                        summary = _shape_summary(shape)
                        if summary:
                            shapes[int(shape.id)] = summary
                            mesh = _mesh_from_shape(shape)
                            if mesh is not None:
                                meshes[int(shape.id)] = mesh
                    except Exception as e:
                        print(f"[Bonsai MCP] geometry skipped for #{shape.id}: {e}")
                    if not iterator.next():
                        break
        cache.update({"file": file, "tree": tree, "shapes": shapes, "count": count, "meshes": meshes})
    return cache


def _aabb_gap(sa, sb):
    gaps = []
    for i in range(3):
        g = max(sb["aabb_min"][i] - sa["aabb_max"][i], sa["aabb_min"][i] - sb["aabb_max"][i], 0.0)
        gaps.append(g)
    return gaps, math.sqrt(sum(g * g for g in gaps))


def _obb_corners(summary):
    import numpy as np
    lmin, lmax = summary["local_min"], summary["local_max"]
    m = np.asarray(summary["matrix"], dtype=float)
    corners = []
    for x in (lmin[0], lmax[0]):
        for y in (lmin[1], lmax[1]):
            for z in (lmin[2], lmax[2]):
                p = m @ np.array([x, y, z, 1.0])
                corners.append(p[:3].tolist())
    # reorder into the same convention as _aabb_corners
    order = [0, 4, 6, 2, 1, 5, 7, 3]
    return [corners[i] for i in order]


def _aabb_corners(mn, mx):
    return [
        (mn[0], mn[1], mn[2]), (mx[0], mn[1], mn[2]), (mx[0], mx[1], mn[2]), (mn[0], mx[1], mn[2]),
        (mn[0], mn[1], mx[2]), (mx[0], mn[1], mx[2]), (mx[0], mx[1], mx[2]), (mn[0], mx[1], mx[2]),
    ]


def _box_segments(c):
    edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
    return [(c[a], c[b]) for a, b in edges]


# ---------------------------------------------------------------------
# geometry tools
# ---------------------------------------------------------------------
def get_element_geometry(global_ids=None, entity_type=None, selected_only=False, limit=100, rebuild_cache=False):
    """Bounding boxes (world AABB + oriented box in the element placement), volume and area."""
    try:
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        elements = _resolve_elements(file, global_ids, entity_type, selected_only)
        if not elements:
            return {"error": "No elements found. Provide global_ids, entity_type or select objects in Blender."}
        cache = _get_geometry_cache(rebuild_cache)
        out, missing = [], []
        gmin, gmax = [math.inf] * 3, [-math.inf] * 3
        for entity in elements[: int(limit)]:
            s = cache["shapes"].get(entity.id())
            if s is None:
                missing.append(_element_info(entity))
                continue
            size = [b - a for a, b in zip(s["aabb_min"], s["aabb_max"])]
            lsize = [b - a for a, b in zip(s["local_min"], s["local_max"])]
            m = s["matrix"]
            info = _element_info(entity)
            info.update({
                "aabb": {
                    "min": _r(s["aabb_min"]), "max": _r(s["aabb_max"]), "size": _r(size),
                    "center": _r([(a + b) / 2 for a, b in zip(s["aabb_min"], s["aabb_max"])]),
                },
                "oriented_box": {
                    "size_local_xyz": _r(lsize),
                    "local_min": _r(s["local_min"]), "local_max": _r(s["local_max"]),
                    "placement_origin": _r([m[0][3], m[1][3], m[2][3]]),
                    "placement_x_axis": _r([m[0][0], m[1][0], m[2][0]]),
                    "placement_z_axis": _r([m[0][2], m[1][2], m[2][2]]),
                },
                "volume_m3": _r(s["volume"]) if s["volume"] is not None else None,
                "surface_area_m2": _r(s["surface_area"]) if s["surface_area"] is not None else None,
                "vertex_count": s["vertex_count"],
                "face_count": s["face_count"],
            })
            out.append(info)
            gmin = [min(a, b) for a, b in zip(gmin, s["aabb_min"])]
            gmax = [max(a, b) for a, b in zip(gmax, s["aabb_max"])]
        result = {"units": "m", "count": len(out), "elements": out}
        if out:
            result["combined_aabb"] = {"min": _r(gmin), "max": _r(gmax), "size": _r([b - a for a, b in zip(gmin, gmax)])}
        if missing:
            result["without_geometry"] = missing
        if len(elements) > int(limit):
            result["truncated"] = f"{len(elements)} elements matched, returned {limit}"
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def measure_distance(global_id_a, global_id_b, tolerance=0.002, draw=False, show_components=False,
                     color=None, rebuild_cache=False):
    """Minimum distance between two elements, closest points and clash status."""
    try:
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        ea = _resolve_elements(file, [global_id_a])
        eb = _resolve_elements(file, [global_id_b])
        if not ea or not eb:
            return {"error": "Element A or B not found"}
        a, b = ea[0], eb[0]
        cache = _get_geometry_cache(rebuild_cache)
        sa, sb = cache["shapes"].get(a.id()), cache["shapes"].get(b.id())
        if sa is None or sb is None:
            return {"error": "Element A or B has no 3D body geometry"}
        tree = cache["tree"]
        gaps, gap_len = _aabb_gap(sa, sb)
        result = {
            "units": "m",
            "a": _element_info(a),
            "b": _element_info(b),
            "aabb_gap_xyz": _r(gaps),
            "aabb_gap": _r(gap_len),
        }
        inter = tree.clash_intersection_many([a], [b], tolerance=float(tolerance), check_all=True)
        if inter:
            worst = max(inter, key=lambda x: x.distance)
            result.update({
                "status": "intersecting",
                "clash_type": tree.get_clash_type(worst.clash_type),
                "penetration_depth": _r(worst.distance),
                "distance": 0.0,
                "point_a": _r(list(worst.p1)),
                "point_b": _r(list(worst.p2)),
            })
        else:
            touching = tree.clash_collision_many([a], [b], allow_touching=False)
            if touching:
                result.update({"status": "touching", "distance": 0.0})
            else:
                corners = [min(x, y) for x, y in zip(sa["aabb_min"], sb["aabb_min"])], \
                          [max(x, y) for x, y in zip(sa["aabb_max"], sb["aabb_max"])]
                upper = math.dist(corners[0], corners[1]) + 1.0
                res = tree.clash_clearance_many([a], [b], clearance=upper, check_all=True)
                if res:
                    best = min(res, key=lambda x: x.distance)
                    p1, p2 = list(best.p1), list(best.p2)
                    result.update({
                        "status": "separate",
                        "distance": _r(best.distance),
                        "point_a": _r(p1),
                        "point_b": _r(p2),
                        "delta_xyz": _r([q - p for p, q in zip(p1, p2)]),
                    })
                else:
                    result.update({"status": "separate", "distance": _r(gap_len), "note": "fallback to AABB gap"})
        if draw and result.get("point_a") and result.get("point_b"):
            label = f"{result['distance']:.3f} m" if result["status"] == "separate" else \
                f"CLASH {result.get('penetration_depth', 0):.3f} m"
            d = _add_dimension(result["point_a"], result["point_b"], label=label,
                               color=color or ("orange" if result["status"] == "separate" else "red"),
                               show_components=show_components and result["status"] == "separate",
                               group=f"distance {a.Name or a.id()} - {b.Name or b.id()}")
            result["drawing_id"] = d
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _is_related(a, b):
    """True for host/filling (via opening), aggregation and nesting relationships."""
    try:
        import ifcopenshell.util.element as ue
        if ue.get_aggregate(a) == b or ue.get_aggregate(b) == a:
            return True
        if ue.get_nest(a) == b or ue.get_nest(b) == a:
            return True
        for x, y in ((a, b), (b, a)):
            for rel in getattr(x, "FillsVoids", []) or []:
                if rel.RelatingOpeningElement.VoidsElements and \
                        rel.RelatingOpeningElement.VoidsElements[0].RelatingBuildingElement == y:
                    return True
    except Exception:
        pass
    return False


def detect_clashes(group_a=None, group_b=None, mode="intersection", tolerance=0.002, clearance=0.05,
                   allow_touching=False, ignore_related=True, limit=200, draw=False, rebuild_cache=False):
    """Clash detection between two groups (IFC classes and/or GlobalIds) using IfcOpenShell's tree."""
    try:
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        cache = _get_geometry_cache(rebuild_cache)
        tree = cache["tree"]

        def group(items):
            if not items:
                return [e for e in file.by_type("IfcElement")
                        if not e.is_a("IfcFeatureElement") and e.id() in cache["shapes"]]
            return [e for e in _resolve_elements(file, items) if e.id() in cache["shapes"]]

        set_a = group(group_a)
        set_b = group(group_b) if group_b else set_a
        if not set_a or not set_b:
            return {"error": "One of the groups has no elements with geometry"}

        mode = (mode or "intersection").lower()
        if mode == "intersection":
            raw = tree.clash_intersection_many(set_a, set_b, tolerance=float(tolerance), check_all=True)
        elif mode == "collision":
            raw = tree.clash_collision_many(set_a, set_b, allow_touching=bool(allow_touching))
        elif mode == "clearance":
            raw = tree.clash_clearance_many(set_a, set_b, clearance=float(clearance), check_all=True)
        else:
            return {"error": "mode must be 'intersection', 'collision' or 'clearance'"}

        pairs = {}
        for c in raw:
            a, b = c.a, c.b
            if a.id() == b.id():
                continue
            key = tuple(sorted((a.id(), b.id())))
            if ignore_related and _is_related(a, b):
                continue
            prev = pairs.get(key)
            better = prev is None or (c.distance < prev.distance if mode == "clearance" else c.distance > prev.distance)
            if better:
                pairs[key] = c

        clashes, summary = [], {}
        for c in sorted(pairs.values(), key=lambda x: -x.distance if mode != "clearance" else x.distance):
            item = {
                "a": _element_info(c.a),
                "b": _element_info(c.b),
                "clash_type": tree.get_clash_type(c.clash_type),
                "distance": _r(c.distance),
                "point_a": _r(list(c.p1)),
                "point_b": _r(list(c.p2)),
            }
            clashes.append(item)
            k = " x ".join(sorted((c.a.is_a(), c.b.is_a())))
            summary[k] = summary.get(k, 0) + 1

        result = {
            "units": "m",
            "mode": mode,
            "distance_meaning": {"intersection": "penetration depth", "collision": "n/a",
                                 "clearance": "gap between elements"}[mode],
            "group_a_count": len(set_a),
            "group_b_count": len(set_b),
            "clash_count": len(clashes),
            "by_type_pair": summary,
            "clashes": clashes[: int(limit)],
        }
        if len(clashes) > int(limit):
            result["truncated"] = f"returned {limit} of {len(clashes)}"
        if draw:
            ids = []
            for item in clashes[: int(limit)]:
                p1, p2 = item["point_a"], item["point_b"]
                label = f"{item['a']['name']} x {item['b']['name']} ({item['distance']:.3f})"
                if math.dist(p1, p2) > 1e-4:
                    ids.append(_add_dimension(p1, p2, label=label, color="red", group=f"clashes {mode}"))
                else:
                    ids.append(_add_marker(p1, label=label, color="red", group=f"clashes {mode}"))
            result["drawing_ids"] = ids
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


# ---------------------------------------------------------------------
# temporary viewport drawings
# ---------------------------------------------------------------------
def _tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def _overlay_draw_3d():
    import gpu
    from gpu_extras.batch import batch_for_shader
    if not _OVERLAY["items"]:
        return
    try:
        shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        fill_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        viewport = gpu.state.viewport_get()
        gpu.state.blend_set("ALPHA")
        for item in _OVERLAY["items"]:
            gpu.state.depth_test_set("NONE" if item.get("on_top", True) else "LESS_EQUAL")
            for tris, color in item.get("fills") or []:
                if not tris:
                    continue
                fill_shader.bind()
                fill_shader.uniform_float("color", color)
                batch_for_shader(fill_shader, "TRIS", {"pos": [p for t in tris for p in t]}).draw(fill_shader)
            for segs, color, width in item["batches"]:
                coords = [p for seg in segs for p in seg]
                shader.bind()
                shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
                shader.uniform_float("lineWidth", width)
                shader.uniform_float("color", color)
                batch_for_shader(shader, "LINES", {"pos": coords}).draw(shader)
        gpu.state.depth_test_set("NONE")
        gpu.state.blend_set("NONE")
    except Exception as e:
        print(f"[Bonsai MCP] overlay 3D draw error: {e}")


def _overlay_draw_2d():
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    if not _OVERLAY["items"]:
        return
    region, rv3d = bpy.context.region, bpy.context.region_data
    if region is None or rv3d is None:
        return
    try:
        font = 0
        ui = bpy.context.preferences.system.ui_scale
        bg_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        for item in _OVERLAY["items"]:
            for text, pos, color in item["labels"]:
                p = location_3d_to_region_2d(region, rv3d, pos)
                if p is None:
                    continue
                blf.size(font, 13 * ui)
                w, h = blf.dimensions(font, text)
                x, y, pad = p.x + 6, p.y + 6, 3 * ui
                rect = [(x - pad, y - pad), (x + w + pad, y - pad), (x + w + pad, y + h + pad), (x - pad, y + h + pad)]
                bg_shader.bind()
                bg_shader.uniform_float("color", (0.0, 0.0, 0.0, 0.65))
                batch_for_shader(bg_shader, "TRI_FAN", {"pos": rect}).draw(bg_shader)
                blf.color(font, *color)
                blf.position(font, x, y, 0)
                blf.draw(font, text)
        gpu.state.blend_set("NONE")
    except Exception as e:
        print(f"[Bonsai MCP] overlay 2D draw error: {e}")


def _remove_overlay_handlers():
    ns = bpy.app.driver_namespace
    handles = ns.get(_OVERLAY_KEY)
    if handles:
        for h in handles:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(h, "WINDOW")
            except Exception:
                pass
    ns[_OVERLAY_KEY] = None


def _ensure_overlay_handlers():
    ns = bpy.app.driver_namespace
    if ns.get(_OVERLAY_KEY):
        return
    ns[_OVERLAY_KEY] = (
        bpy.types.SpaceView3D.draw_handler_add(_overlay_draw_3d, (), "WINDOW", "POST_VIEW"),
        bpy.types.SpaceView3D.draw_handler_add(_overlay_draw_2d, (), "WINDOW", "POST_PIXEL"),
    )


def _add_item(kind, batches, labels, group=None, on_top=True, meta=None, fills=None):
    _ensure_overlay_handlers()
    item_id = _OVERLAY["next_id"]
    _OVERLAY["next_id"] += 1
    _OVERLAY["items"].append({
        "id": item_id, "kind": kind, "group": group, "on_top": on_top,
        "batches": batches, "labels": labels, "meta": meta or {}, "fills": fills or [],
    })
    _tag_redraw()
    return item_id


def _add_dimension(start, end, label=None, color=None, width=2.5, show_components=False,
                   group=None, on_top=True):
    from mathutils import Vector
    p1, p2 = Vector([float(v) for v in start]), Vector([float(v) for v in end])
    col = _parse_color(color)
    d = p2 - p1
    length = d.length
    segs = [(tuple(p1), tuple(p2))]
    if length > 1e-6:
        ref = Vector((0, 0, 1)) if abs(d.normalized().z) < 0.9 else Vector((1, 0, 0))
        perp = d.cross(ref).normalized() * max(0.05, min(0.25, length * 0.04))
        for p in (p1, p2):
            segs.append((tuple(p - perp), tuple(p + perp)))
    batches = [(segs, col, float(width))]
    text = label if label else f"{length:.3f} m"
    labels = [(text, tuple((p1 + p2) / 2), (1, 1, 1, 1))]
    if show_components and length > 1e-6:
        corner1 = Vector((p2.x, p1.y, p1.z))
        corner2 = Vector((p2.x, p2.y, p1.z))
        legs = [(p1, corner1, 0, "Δx"), (corner1, corner2, 1, "Δy"), (corner2, p2, 2, "Δz")]
        for a, b, axis, name in legs:
            delta = (b - a).length
            if delta < 1e-6:
                continue
            batches.append(([(tuple(a), tuple(b))], _AXIS_COLORS[axis], 1.5))
            labels.append((f"{name} {delta:.3f}", tuple((a + b) / 2), _AXIS_COLORS[axis]))
    return _add_item("dimension", batches, labels, group, on_top,
                     {"start": _r(list(p1)), "end": _r(list(p2)), "length": _r(length)})


def _add_marker(point, label=None, color=None, size=0.15, group=None):
    p = [float(v) for v in point]
    col = _parse_color(color, "red")
    segs = []
    for axis in range(3):
        a, b = list(p), list(p)
        a[axis] -= size
        b[axis] += size
        segs.append((tuple(a), tuple(b)))
    labels = [(label, tuple(p), (1, 1, 1, 1))] if label else []
    return _add_item("marker", [(segs, col, 3.0)], labels, group, True, {"point": _r(p)})


def draw_dimension(start, end, label=None, color=None, width=2.5, show_components=False, on_top=True, group=None):
    """Draw a temporary dimension line between two world points (metres)."""
    try:
        start, end = _as_list(start), _as_list(end)
        if len(start) != 3 or len(end) != 3:
            return {"error": "start and end must be [x, y, z]"}
        item_id = _add_dimension(start, end, label, color, width, show_components, group or "dimension", on_top)
        return {"drawing_id": item_id, "length": _OVERLAY["items"][-1]["meta"]["length"], "units": "m"}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def draw_bounding_boxes(global_ids=None, entity_type=None, selected_only=False, oriented=False,
                        color=None, show_labels=True, limit=200, rebuild_cache=False):
    """Draw temporary bounding boxes (world AABB or oriented box) around elements."""
    try:
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        elements = _resolve_elements(file, global_ids, entity_type, selected_only)
        if not elements:
            return {"error": "No elements found. Provide global_ids, entity_type or select objects in Blender."}
        cache = _get_geometry_cache(rebuild_cache)
        col = _parse_color(color, "cyan")
        ids = []
        for entity in elements[: int(limit)]:
            s = cache["shapes"].get(entity.id())
            if s is None:
                continue
            if oriented:
                corners = _obb_corners(s)
                size = [b - a for a, b in zip(s["local_min"], s["local_max"])]
            else:
                corners = _aabb_corners(s["aabb_min"], s["aabb_max"])
                size = [b - a for a, b in zip(s["aabb_min"], s["aabb_max"])]
            labels = []
            if show_labels:
                top = max(corners, key=lambda c: (c[2], c[0] + c[1]))
                labels.append((f"{entity.Name or entity.is_a()}  {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f}",
                               tuple(top), (1, 1, 1, 1)))
            ids.append(_add_item("box", [(_box_segments(corners), col, 1.5)], labels,
                                 f"boxes{' oriented' if oriented else ''}", True,
                                 {"element": entity.GlobalId, "size": _r(size)}))
        return {"drawn": len(ids), "drawing_ids": ids, "oriented": bool(oriented)}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def clear_temporary_drawings(drawing_ids=None, group=None, list_only=False):
    """Remove temporary drawings (all, by id or by group). With list_only=True just lists them."""
    try:
        items = _OVERLAY["items"]
        if not list_only:
            ids = {int(i) for i in _as_list(drawing_ids)}
            if not ids and not group:
                removed = len(items)
                _OVERLAY["items"] = []
            else:
                keep = [i for i in items if not (i["id"] in ids or (group and i["group"] == group))]
                removed = len(items) - len(keep)
                _OVERLAY["items"] = keep
            if not _OVERLAY["items"]:
                _remove_overlay_handlers()
            _tag_redraw()
        else:
            removed = 0
        return {
            "removed": removed,
            "remaining": [{"id": i["id"], "kind": i["kind"], "group": i["group"], **i["meta"]}
                          for i in _OVERLAY["items"]],
        }
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


# ---------------------------------------------------------------------
# space boundaries (IfcRelSpaceBoundary)
# ---------------------------------------------------------------------
_BOUNDARY_CANDIDATE_CLASSES = (
    "IfcWall", "IfcSlab", "IfcRoof", "IfcCovering", "IfcDoor", "IfcWindow", "IfcColumn",
    "IfcBeam", "IfcCurtainWall", "IfcPlate", "IfcMember", "IfcStair", "IfcRamp",
)
_POSITION_COLORS = {
    "floor": (0.85, 0.55, 0.15, 1.0), "ceiling": (0.25, 0.55, 1.0, 1.0),
    "wall": (0.2, 0.8, 0.35, 1.0), "inclined": (0.85, 0.3, 0.85, 1.0), "unknown": (0.6, 0.6, 0.6, 1.0),
}
_IE_COLORS = {
    "INTERNAL": (0.2, 0.8, 0.35, 1.0), "EXTERNAL": (1.0, 0.45, 0.0, 1.0),
    "EXTERNAL_EARTH": (0.55, 0.35, 0.15, 1.0), "EXTERNAL_WATER": (0.1, 0.5, 0.9, 1.0),
    "EXTERNAL_FIRE": (0.95, 0.1, 0.1, 1.0), "NOTDEFINED": (0.6, 0.6, 0.6, 1.0),
}
_CLASS_PALETTE = [
    (0.9, 0.35, 0.3, 1.0), (0.3, 0.7, 0.95, 1.0), (0.95, 0.75, 0.2, 1.0), (0.45, 0.85, 0.4, 1.0),
    (0.75, 0.45, 0.95, 1.0), (0.95, 0.5, 0.75, 1.0), (0.4, 0.9, 0.85, 1.0), (0.7, 0.7, 0.7, 1.0),
]


def _effective_predefined_type(element):
    """PredefinedType following the type/occurrence rule (type wins unless NOTDEFINED/None;
    USERDEFINED resolves to ElementType / ObjectType)."""
    import ifcopenshell.util.element as ue
    if element is None:
        return None, None
    etype = ue.get_type(element)
    type_value = getattr(etype, "PredefinedType", None) if etype else None
    inst_value = getattr(element, "PredefinedType", None)
    if type_value and type_value != "NOTDEFINED":
        if type_value == "USERDEFINED":
            return getattr(etype, "ElementType", None) or "USERDEFINED", "type"
        return type_value, "type"
    if inst_value:
        if inst_value == "USERDEFINED":
            return getattr(element, "ObjectType", None) or "USERDEFINED", "occurrence"
        return inst_value, "occurrence"
    return None, None


def _curve_points_2d(curve):
    if curve.is_a("IfcPolyline"):
        pts = [tuple(p.Coordinates[:2]) for p in curve.Points]
    elif curve.is_a("IfcIndexedPolyCurve"):
        coords = [tuple(c[:2]) for c in curve.Points.CoordList]
        if curve.Segments:
            order = []
            for seg in curve.Segments:
                for i in seg.wrappedValue:
                    if not order or order[-1] != i - 1:
                        order.append(i - 1)
            pts = [coords[i] for i in order]
        else:
            pts = coords
    elif curve.is_a("IfcCompositeCurve"):
        pts = []
        for seg in curve.Segments:
            sub = _curve_points_2d(seg.ParentCurve)
            if seg.SameSense is False:
                sub = list(reversed(sub))
            for p in sub:
                if not pts or pts[-1] != p:
                    pts.append(p)
    else:
        raise ValueError(f"unsupported boundary curve {curve.is_a()}")
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
        pts = pts[:-1]
    return pts


def _polygon_area_2d(pts):
    return 0.5 * abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1])))


def _boundary_geometry(rel, scale):
    """World geometry (metres) of a boundary's SurfaceOnRelatingElement."""
    import numpy as np
    import ifcopenshell.util.placement as up
    cg = rel.ConnectionGeometry
    srf = getattr(cg, "SurfaceOnRelatingElement", None) if cg else None
    if srf is None:
        return None
    space = rel.RelatingSpace
    ms = up.get_local_placement(space.ObjectPlacement) if getattr(space, "ObjectPlacement", None) else np.eye(4)
    if srf.is_a("IfcCurveBoundedPlane"):
        mp = up.get_axis2placement(srf.BasisSurface.Position)
        m = ms @ mp
        loops, area = [], 0.0
        curves = [srf.OuterBoundary] + list(srf.InnerBoundaries or [])
        for k, curve in enumerate(curves):
            pts2 = _curve_points_2d(curve)
            a = _polygon_area_2d(pts2)
            area += a if k == 0 else -a
            loops.append([((m @ np.array([x, y, 0.0, 1.0]))[:3] * scale).tolist() for x, y in pts2])
        normal = (m[:3, :3] @ np.array([0.0, 0.0, 1.0]))
        normal = normal / (np.linalg.norm(normal) or 1.0)
        return {"loops": loops, "triangles": None, "area": area * scale * scale,
                "normal": normal.tolist(), "centroid": np.mean(np.array(loops[0]), axis=0).tolist()}
    # generic fallback: triangulate with the geometry kernel
    shape = ifcopenshell.geom.create_shape(ifcopenshell.geom.settings(), srf)
    v = np.asarray(shape.verts, dtype=float).reshape(-1, 3)
    v = (np.c_[v, np.ones(len(v))] @ ms.T)[:, :3] * scale
    faces = np.asarray(shape.faces, dtype=int).reshape(-1, 3)
    tri = v[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = float(0.5 * np.linalg.norm(cross, axis=1).sum())
    n = cross.sum(axis=0)
    n = n / (np.linalg.norm(n) or 1.0)
    return {"loops": None, "triangles": tri.tolist(), "area": area, "normal": n.tolist(),
            "centroid": v.mean(axis=0).tolist()}


def _boundary_position(geo, space_summary):
    if geo is None:
        return "unknown"
    nz = geo["normal"][2]
    if abs(nz) < 0.3:
        return "wall"
    if abs(nz) < 0.85:
        return "inclined"
    if space_summary:
        mid = (space_summary["aabb_min"][2] + space_summary["aabb_max"][2]) / 2
        return "floor" if geo["centroid"][2] < mid else "ceiling"
    return "floor" if nz < 0 else "ceiling"


def _resolve_spaces(file, space_ids=None):
    items = _as_list(space_ids)
    if not items:
        return list(file.by_type("IfcSpace"))
    spaces = [e for e in _resolve_elements(file, items) if e.is_a("IfcSpatialElement")]
    found_ids = {s.id() for s in spaces}
    for item in items:
        key = str(item).strip().lower()
        for s in file.by_type("IfcSpace"):
            if s.id() not in found_ids and key in ((s.Name or "").lower(), (s.LongName or "").lower()):
                spaces.append(s)
                found_ids.add(s.id())
    return spaces


def _space_label(space):
    return f"{space.Name or ''} {('- ' + space.LongName) if getattr(space, 'LongName', None) else ''}".strip()


def _collect_boundaries(file, space, cache, scale):
    space_summary = cache["shapes"].get(space.id()) if cache else None
    records = []
    for rel in space.BoundedBy or []:
        el = rel.RelatedBuildingElement
        try:
            geo = _boundary_geometry(rel, scale)
            geo_error = None
        except Exception as e:
            geo, geo_error = None, str(e)
        ptype, psrc = _effective_predefined_type(el)
        rec = {
            "boundary_id": rel.id(),
            "class": rel.is_a(),
            "element": _element_info(el) if el else None,
            "element_predefined_type": ptype,
            "predefined_type_source": psrc,
            "physical_or_virtual": rel.PhysicalOrVirtualBoundary,
            "internal_or_external": rel.InternalOrExternalBoundary,
            "position": _boundary_position(geo, space_summary),
            "area_m2": _r(geo["area"]) if geo else None,
            "normal": _r(geo["normal"], 3) if geo else None,
            "centroid": _r(geo["centroid"]) if geo else None,
            "connection_geometry": rel.ConnectionGeometry.is_a() if rel.ConnectionGeometry else None,
        }
        if geo_error:
            rec["geometry_error"] = geo_error
        if hasattr(rel, "ParentBoundary"):
            rec["parent_boundary"] = rel.ParentBoundary.id() if rel.ParentBoundary else None
            rec["corresponding_boundary"] = rel.CorrespondingBoundary.id() if rel.CorrespondingBoundary else None
            rec["inner_boundaries"] = [b.id() for b in getattr(rel, "InnerBoundaries", []) or []]
        rec["_geo"] = geo
        rec["_rel"] = rel
        records.append(rec)
    return records


def _strip_private(records):
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]


def get_space_boundaries(space_ids=None, rebuild_cache=False):
    """List the IfcRelSpaceBoundary of each space with element, flags, position, area and normal."""
    try:
        import ifcopenshell.util.unit as uu
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        cache = _get_geometry_cache(rebuild_cache)
        scale = uu.calculate_unit_scale(file)
        out = []
        for space in spaces:
            recs = _collect_boundaries(file, space, cache, scale)
            by_class, area_by_pos, ie, pv = {}, {}, {}, {}
            for r in recs:
                cls = r["element"]["type"] if r["element"] else "(none)"
                by_class[cls] = by_class.get(cls, 0) + 1
                area_by_pos[r["position"]] = area_by_pos.get(r["position"], 0.0) + (r["area_m2"] or 0.0)
                ie[r["internal_or_external"]] = ie.get(r["internal_or_external"], 0) + 1
                pv[r["physical_or_virtual"]] = pv.get(r["physical_or_virtual"], 0) + 1
            s = cache["shapes"].get(space.id())
            out.append({
                "space": _element_info(space) | {"long_name": getattr(space, "LongName", None)},
                "boundary_count": len(recs),
                "by_element_class": by_class,
                "area_by_position_m2": {k: _r(v, 3) for k, v in area_by_pos.items()},
                "space_geometry_floor_area_m2": _r(s.get("bottom_area"), 3) if s and s.get("bottom_area") is not None else None,
                "internal_or_external": ie,
                "physical_or_virtual": pv,
                "boundaries": _strip_private(recs),
            })
        return {"units": "m", "total_boundaries": len(file.by_type("IfcRelSpaceBoundary")),
                "space_count": len(out), "spaces": out}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _contact_size(sa, sb, proximity):
    """Approximate contact patch between two AABBs: the two largest overlaps (m)."""
    ov = []
    for i in range(3):
        lo = max(sa["aabb_min"][i], sb["aabb_min"][i] - proximity)
        hi = min(sa["aabb_max"][i], sb["aabb_max"][i] + proximity)
        ov.append(max(0.0, hi - lo))
    ov.sort(reverse=True)
    return ov[0], ov[1]


def validate_space_boundaries(space_ids=None, proximity=0.03, area_tolerance=0.05,
                              candidate_classes=None, min_contact=0.10, limit=300, rebuild_cache=False):
    """Check IfcRelSpaceBoundary consistency (attributes, geometry, hosts, coverings, areas)
    and compare declared boundaries with the elements that geometrically touch each space."""
    try:
        import ifcopenshell.util.unit as uu
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        cache = _get_geometry_cache(rebuild_cache)
        tree = cache["tree"]
        scale = uu.calculate_unit_scale(file)
        classes = tuple(_as_list(candidate_classes)) or _BOUNDARY_CANDIDATE_CLASSES
        candidates = [e for e in file.by_type("IfcElement")
                      if e.id() in cache["shapes"] and any(e.is_a(c) for c in classes)]
        issues = []

        def add(severity, code, space, message, boundary=None, element=None, **extra):
            item = {"severity": severity, "code": code, "space": _space_label(space), "message": message}
            if boundary is not None:
                item["boundary_id"] = boundary
            if element is not None:
                item["element"] = _element_info(element)
            item.update(extra)
            issues.append(item)

        expected_cov = {"floor": ("FLOORING",), "ceiling": ("CEILING",), "wall": ("CLADDING", "INSULATION", "MOLDING", "SKIRTINGBOARD")}
        per_space = []
        for space in spaces:
            recs = _collect_boundaries(file, space, cache, scale)
            s_sum = cache["shapes"].get(space.id())
            if not recs:
                add("error", "E01", space, "Space has no IfcRelSpaceBoundary")
            declared = {}
            for r in recs:
                rel, el, bid = r["_rel"], r["_rel"].RelatedBuildingElement, r["boundary_id"]
                if el is not None:
                    declared.setdefault(el.id(), []).append(r)
                if rel.ConnectionGeometry is None:
                    add("error", "E02", space, "Boundary without ConnectionGeometry", bid, el)
                elif r.get("geometry_error"):
                    add("warning", "W02", space, f"Boundary geometry could not be read: {r['geometry_error']}", bid, el)
                if rel.PhysicalOrVirtualBoundary == "PHYSICAL" and el is None:
                    add("error", "E03", space, "PHYSICAL boundary without RelatedBuildingElement", bid)
                if rel.PhysicalOrVirtualBoundary == "VIRTUAL" and el is not None and not el.is_a("IfcVirtualElement"):
                    add("warning", "W04", space, f"VIRTUAL boundary related to a physical element ({el.is_a()})", bid, el)
                if rel.PhysicalOrVirtualBoundary in (None, "NOTDEFINED"):
                    add("warning", "W05", space, "PhysicalOrVirtualBoundary is NOTDEFINED", bid, el)
                if rel.InternalOrExternalBoundary in (None, "NOTDEFINED"):
                    add("warning", "W06", space, "InternalOrExternalBoundary is NOTDEFINED", bid, el)
                if r["area_m2"] is not None and r["area_m2"] < 1e-4:
                    add("warning", "W07", space, "Boundary with zero area", bid, el)
                # opening fillers must sit on a boundary of their host
                if el is not None and (el.is_a("IfcDoor") or el.is_a("IfcWindow")):
                    hosts = []
                    for fv in getattr(el, "FillsVoids", []) or []:
                        for ve in fv.RelatingOpeningElement.VoidsElements or []:
                            hosts.append(ve.RelatingBuildingElement)
                    if hosts and not any(h.id() in {x["_rel"].RelatedBuildingElement.id() for x in recs if x["_rel"].RelatedBuildingElement} for h in hosts):
                        add("warning", "W09", space, f"{el.is_a()} boundary without a boundary of its host ({', '.join(h.Name or h.is_a() for h in hosts)})", bid, el)
                    if r.get("parent_boundary", "n/a") is None:
                        add("warning", "W16", space, "Opening boundary (2nd level) without ParentBoundary", bid, el)
                # covering type vs boundary position
                if el is not None and el.is_a("IfcCovering") and r["position"] in expected_cov:
                    ptype = r["element_predefined_type"]
                    if ptype not in expected_cov[r["position"]]:
                        add("warning", "W10", space,
                            f"Covering with PredefinedType {ptype} ({r['predefined_type_source']}) bounds the {r['position']}; expected {'/'.join(expected_cov[r['position']])}",
                            bid, el)
                if r.get("corresponding_boundary"):
                    cb = file.by_id(r["corresponding_boundary"])
                    if getattr(cb, "CorrespondingBoundary", None) is None or cb.CorrespondingBoundary.id() != bid:
                        add("warning", "W15", space, "CorrespondingBoundary is not reciprocal", bid, el)
            # duplicates
            for el_id, lst in declared.items():
                for i in range(len(lst)):
                    for j in range(i + 1, len(lst)):
                        a, b = lst[i], lst[j]
                        if a["centroid"] and b["centroid"] and math.dist(a["centroid"], b["centroid"]) < 0.01 \
                                and abs((a["area_m2"] or 0) - (b["area_m2"] or 0)) <= 0.01 * max(a["area_m2"] or 1, 1e-6):
                            add("warning", "W08", space, "Duplicated boundary (same element, same surface)",
                                b["boundary_id"], file.by_id(el_id), duplicate_of=a["boundary_id"])
            # positions present
            positions = {r["position"] for r in recs}
            for pos in ("floor", "ceiling", "wall"):
                if recs and pos not in positions:
                    add("warning", "W14", space, f"No {pos} boundary")
            # areas vs space geometry
            if s_sum and recs:
                for pos, key in (("floor", "bottom_area"), ("ceiling", "top_area")):
                    ref = s_sum.get(key)
                    at_pos = [r for r in recs if r["position"] == pos]
                    cov = sum(r["area_m2"] or 0 for r in at_pos if r["_rel"].RelatedBuildingElement
                              and r["_rel"].RelatedBuildingElement.is_a("IfcCovering"))
                    other = sum(r["area_m2"] or 0 for r in at_pos) - cov
                    tot = max(cov, other)  # covering and slab may both bound the same face
                    if ref and tot and abs(tot - ref) / ref > float(area_tolerance):
                        add("warning", "W13", space,
                            f"Sum of {pos} boundary areas ({tot:.2f} m2) differs from space geometry ({ref:.2f} m2)",
                            boundary_area=_r(tot, 3), space_area=_r(ref, 3))
            # geometry: declared vs touching
            adjacent = {}
            if space.id() in cache["shapes"] and candidates:
                s_box = cache["shapes"][space.id()]
                for c in tree.clash_clearance_many([space], candidates, clearance=float(proximity) + 1e-4, check_all=True):
                    other = c.b if c.a.id() == space.id() else c.a
                    adjacent[other.id()] = min(adjacent.get(other.id(), 1e9), c.distance)
                for c in tree.clash_collision_many([space], candidates, allow_touching=False):
                    other = c.b if c.a.id() == space.id() else c.a
                    adjacent[other.id()] = 0.0
                for el_id, dist in sorted(adjacent.items(), key=lambda x: x[1]):
                    if el_id in declared:
                        continue
                    w, h = _contact_size(s_box, cache["shapes"][el_id], float(proximity))
                    if h < float(min_contact):
                        continue  # only an edge/corner contact
                    add("info", "I12", space,
                        f"Element touches the space (≤ {proximity} m, contact ≈ {w:.2f} x {h:.2f} m) but has no boundary",
                        element=file.by_id(el_id), distance=_r(dist))
                adjacent_coverings = {i for i in adjacent if file.by_id(i).is_a("IfcCovering")}
                for el_id, lst in declared.items():
                    if el_id in adjacent:
                        continue
                    el = file.by_id(el_id)
                    if el_id not in cache["shapes"]:
                        continue
                    res = tree.clash_clearance_many([space], [el], clearance=2.0, check_all=True)
                    d = min((x.distance for x in res), default=None)
                    covers = set()
                    for rel in getattr(el, "HasCoverings", []) or []:
                        covers |= {c.id() for c in rel.RelatedCoverings}
                    behind = covers & adjacent_coverings
                    how = "IfcRelCoversBldgElements"
                    if not behind and adjacent_coverings:
                        near = tree.clash_clearance_many([el], [file.by_id(i) for i in adjacent_coverings],
                                                         clearance=float(proximity) + 1e-4, check_all=True)
                        behind = {(c.b if c.a.id() == el.id() else c.a).id() for c in near}
                        how = "geometric contact"
                    hosts_in = [h for h in _host_elements(el) if h.id() in declared or h.id() in adjacent]
                    if (el.is_a("IfcDoor") or el.is_a("IfcWindow")) and hosts_in:
                        add("info", "I18", space,
                            f"Opening recessed in its host wall ({', '.join(h.Name or '?' for h in hosts_in)}) at {d:.3f} m",
                            lst[0]["boundary_id"], el, distance=_r(d) if d is not None else None)
                    elif behind:
                        add("info", "I17", space,
                            f"Boundary element sits behind a covering ({', '.join(file.by_id(i).Name or '?' for i in behind)}; {how}) at {d:.3f} m",
                            lst[0]["boundary_id"], el, distance=_r(d) if d is not None else None)
                    else:
                        add("warning", "W11", space,
                            f"Boundary element is not adjacent to the space geometry ({'%.3f m' % d if d is not None else '> 2 m'})",
                            lst[0]["boundary_id"], el, distance=_r(d) if d is not None else None)
            per_space.append({"space": _space_label(space), "boundaries": len(recs),
                              "geometrically_adjacent_elements": len(adjacent)})

        by_code = {}
        for i in issues:
            by_code[i["code"]] = by_code.get(i["code"], 0) + 1
        legend = {
            "E01": "space without boundaries", "E02": "no ConnectionGeometry", "E03": "PHYSICAL without element",
            "W02": "geometry unreadable", "W04": "VIRTUAL with physical element", "W05": "Physical/Virtual NOTDEFINED",
            "W06": "Internal/External NOTDEFINED", "W07": "zero area", "W08": "duplicate boundary",
            "W09": "door/window without host boundary", "W10": "covering PredefinedType vs position",
            "W11": "declared element not adjacent", "W13": "boundary area vs space area", "W14": "missing floor/ceiling/wall",
            "W15": "CorrespondingBoundary not reciprocal", "W16": "opening without ParentBoundary",
            "I12": "adjacent element without boundary", "I17": "declared element behind a covering",
            "I18": "door/window recessed in host wall",
        }
        order = {"error": 0, "warning": 1, "info": 2}
        issues.sort(key=lambda i: (order[i["severity"]], i["space"], i["code"]))
        result = {
            "units": "m", "space_count": len(spaces), "issue_count": len(issues),
            "by_severity": {s: sum(1 for i in issues if i["severity"] == s) for s in ("error", "warning", "info")},
            "by_code": {f"{k} {legend.get(k, '')}": v for k, v in sorted(by_code.items())},
            "per_space": per_space,
            "issues": issues[: int(limit)],
        }
        if len(issues) > int(limit):
            result["truncated"] = f"returned {limit} of {len(issues)}"
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def draw_space_boundaries(space_ids=None, color_by="position", fill=True, opacity=0.35,
                          show_labels=False, element_ids=None, rebuild_cache=False):
    """Draw the boundary surfaces of spaces as temporary translucent polygons with outlines."""
    try:
        import ifcopenshell.util.unit as uu
        from mathutils import Vector
        from mathutils.geometry import tessellate_polygon
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        only = {e.id() for e in _resolve_elements(file, element_ids)} if element_ids else None
        cache = _get_geometry_cache(rebuild_cache)
        scale = uu.calculate_unit_scale(file)
        class_colors, legend, ids = {}, {}, []
        for space in spaces:
            recs = _collect_boundaries(file, space, cache, scale)
            for r in recs:
                geo = r["_geo"]
                el = r["_rel"].RelatedBuildingElement
                if geo is None or (only is not None and (el is None or el.id() not in only)):
                    continue
                if color_by == "internal_external":
                    key = r["internal_or_external"] or "NOTDEFINED"
                    col = _IE_COLORS.get(key, _IE_COLORS["NOTDEFINED"])
                elif color_by == "element_class":
                    key = el.is_a() if el else "(virtual)"
                    if key not in class_colors:
                        class_colors[key] = _CLASS_PALETTE[len(class_colors) % len(_CLASS_PALETTE)]
                    col = class_colors[key]
                else:
                    key = r["position"]
                    col = _POSITION_COLORS.get(key, _POSITION_COLORS["unknown"])
                legend[key] = _r(list(col), 2)
                fill_col = (col[0], col[1], col[2], float(opacity))
                segs, fills = [], []
                if geo["loops"]:
                    for loop in geo["loops"]:
                        segs += [(tuple(loop[i]), tuple(loop[(i + 1) % len(loop)])) for i in range(len(loop))]
                    if fill:
                        vecs = [[Vector(p) for p in loop] for loop in geo["loops"]]
                        flat = [v for loop in vecs for v in loop]
                        for tri in tessellate_polygon(vecs):
                            fills.append([tuple(flat[i]) for i in tri])
                else:
                    for t in geo["triangles"]:
                        segs += [(tuple(t[0]), tuple(t[1])), (tuple(t[1]), tuple(t[2])), (tuple(t[2]), tuple(t[0]))]
                        if fill:
                            fills.append([tuple(p) for p in t])
                labels = []
                if show_labels:
                    labels.append((f"{(el.Name if el else 'virtual')} {r['area_m2'] or 0:.2f} m²",
                                   tuple(geo["centroid"]), (1, 1, 1, 1)))
                ids.append(_add_item("boundary", [(segs, col, 1.5)], labels,
                                     f"boundaries {_space_label(space)}", True,
                                     {"boundary_id": r["boundary_id"], "space": _space_label(space),
                                      "element": el.GlobalId if el else None, "key": key},
                                     fills=[(fills, fill_col)] if fills else None))
        return {"drawn": len(ids), "drawing_ids": ids, "color_by": color_by, "legend": legend}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


# ---------------------------------------------------------------------
# space boundary generation (2nd level) and attribute fixing
# ---------------------------------------------------------------------
_GEN_TAG = "MCP-generated"


def _mesh_from_shape(shape):
    """World triangles (metres) with normals/areas/centroids from an iterator shape."""
    import numpy as np
    import ifcopenshell.util.shape as ushape
    try:
        v = np.asarray(ushape.get_shape_vertices(shape, shape.geometry), dtype=float)
        f = np.asarray(shape.geometry.faces, dtype=int).reshape(-1, 3)
    except Exception:
        return None
    if not len(v) or not len(f):
        return None
    tri = v[f]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    ok = norm > 1e-12
    tri, cross, norm = tri[ok], cross[ok], norm[ok]
    return {"tri": tri, "normal": cross / norm[:, None], "area": 0.5 * norm, "centroid": tri.mean(axis=1)}


def _mesh_world(entity):
    """World triangles of an element, taken from the geometry cache (same OpenCASCADE pass as the tree)."""
    mesh = _GEOM_CACHE.get("meshes", {}).get(entity.id())
    if mesh is None:
        raise ValueError(f"no cached geometry for #{entity.id()} ({entity.is_a()})")
    return mesh


def _ray_hits(origin, direction, entities, max_dist):
    """Ray cast (Möller–Trumbore) against element meshes. Returns [(t, entity)] sorted."""
    import numpy as np
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    d = d / (np.linalg.norm(d) or 1.0)
    hits = []
    for e in entities:
        s = _GEOM_CACHE["shapes"].get(e.id())
        if s is not None:  # slab test against the AABB
            tmin, tmax = 0.0, float(max_dist)
            inside = True
            for i in range(3):
                lo, hi = s["aabb_min"][i] - 1e-3, s["aabb_max"][i] + 1e-3
                if abs(d[i]) < 1e-12:
                    if o[i] < lo or o[i] > hi:
                        inside = False
                        break
                else:
                    t1, t2 = (lo - o[i]) / d[i], (hi - o[i]) / d[i]
                    tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
                    if tmin > tmax:
                        inside = False
                        break
            if not inside:
                continue
        try:
            tri = _mesh_world(e)["tri"]
        except Exception:
            continue
        e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        p = np.cross(d, e2)
        det = (e1 * p).sum(axis=1)
        ok = np.abs(det) > 1e-12
        inv = np.zeros_like(det)
        inv[ok] = 1.0 / det[ok]
        tv = o - tri[:, 0]
        u = (tv * p).sum(axis=1) * inv
        q = np.cross(tv, e1)
        v = (q * d).sum(axis=1) * inv
        t = (e2 * q).sum(axis=1) * inv
        mask = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-6) & (t <= max_dist)
        if mask.any():
            hits.append((float(t[mask].min()), e))
    hits.sort(key=lambda h: h[0])
    return hits


def _frame(normal, origin):
    import numpy as np
    n = np.asarray(normal, dtype=float)
    n = n / np.linalg.norm(n)
    if abs(n[2]) < 0.9:
        u = np.cross(np.array([0.0, 0.0, 1.0]), n)
    else:
        u = np.array([1.0, 0.0, 0.0]) - n[0] * n
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    return {"o": np.asarray(origin, dtype=float), "u": u, "v": v, "n": n}


def _to2d(fr, pts):
    import numpy as np
    p = np.asarray(pts, dtype=float) - fr["o"]
    return np.c_[p @ fr["u"], p @ fr["v"]]


def _to3d(fr, pts2):
    import numpy as np
    q = np.asarray(pts2, dtype=float)
    return fr["o"] + q[:, :1] * fr["u"] + q[:, 1:2] * fr["v"]


def _parts(geom, min_area, min_width=0.02):
    """Polygon parts with area >= min_area and not thinner than min_width (slivers)."""
    if geom is None or geom.is_empty:
        return []
    geoms = getattr(geom, "geoms", [geom])
    out = []
    for g in geoms:
        if g.geom_type == "Polygon":
            if g.area >= min_area and not g.buffer(-min_width / 2.0).is_empty:
                out.append(g)
        elif hasattr(g, "geoms"):
            out += _parts(g, min_area, min_width)
    return out


def _position_from_normal(n):
    if abs(n[2]) < 0.3:
        return "wall"
    if abs(n[2]) < 0.85:
        return "inclined"
    return "floor" if n[2] < 0 else "ceiling"


def _space_faces(space, min_area):
    """Planar faces of a space solid (outward normals) as shapely polygons in a local frame."""
    import numpy as np
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.affinity import translate
    m = _mesh_world(space)
    groups = []
    for i, (n, c) in enumerate(zip(m["normal"], m["centroid"])):
        for g in groups:
            if float(n @ g["n"]) > 0.9995 and abs(float(n @ c) - g["d"]) < 0.003:
                g["idx"].append(i)
                break
        else:
            groups.append({"n": n.copy(), "d": float(n @ c), "idx": [i]})
    faces = []
    for g in groups:
        fr = _frame(g["n"], g["n"] * g["d"])
        polys = [Polygon(_to2d(fr, m["tri"][i])) for i in g["idx"]]
        poly = unary_union([p.buffer(1e-6) for p in polys if p.area > 1e-12]).buffer(-1e-6).simplify(1e-5)
        if poly.is_empty or poly.area < min_area:
            continue
        minx, miny = poly.bounds[0], poly.bounds[1]
        fr["o"] = fr["o"] + minx * fr["u"] + miny * fr["v"]
        poly = translate(poly, -minx, -miny)
        faces.append({"frame": fr, "poly": poly, "position": _position_from_normal(fr["n"])})
    return faces


def _footprint(face, element, search_depth, tol, min_area):
    """Projection on a space face of the element triangles that face the space."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    fr = face["frame"]
    m = _mesh_world(element)
    dots = m["normal"] @ fr["n"]
    depth = (m["centroid"] - fr["o"]) @ fr["n"]
    sel = (dots < -0.985) & (depth >= -tol) & (depth <= search_depth)
    if not sel.any():
        return None
    polys = [Polygon(_to2d(fr, t)) for t in m["tri"][sel]]
    fp = unary_union([p.buffer(1e-6) for p in polys if p.area > 1e-12])
    if fp.intersection(face["poly"]).area < min_area:
        return None
    return fp, float(depth[sel].min())


def _is_external_space(s):
    return s.is_a("IfcExternalSpatialElement") or getattr(s, "PredefinedType", None) == "EXTERNAL"


def _classify_side(space, point, normal, other_spaces, geo_elements, max_thickness):
    """INTERNAL / EXTERNAL / EXTERNAL_EARTH by casting a ray from the boundary outwards."""
    import numpy as np
    p = np.asarray(point, dtype=float) + np.asarray(normal, dtype=float) * 1e-3
    hits = _ray_hits(p, normal, [s for s in other_spaces if s.id() != space.id()], max_thickness)
    if hits:
        t, s2 = hits[0]
        ie = "EXTERNAL" if (_is_external_space(space) or _is_external_space(s2)) else "INTERNAL"
        return ie, s2, t
    if geo_elements and _ray_hits(p, normal, geo_elements, max_thickness):
        return "EXTERNAL_EARTH", None, None
    return "EXTERNAL", None, None


def _overlap_ratio(fr, poly, other_fr, other_poly):
    """Overlap of other_poly projected onto fr's plane, relative to the smaller area."""
    from shapely.geometry import Polygon
    ext = _to2d(fr, _to3d(other_fr, list(other_poly.exterior.coords)))
    holes = [_to2d(fr, _to3d(other_fr, list(r.coords))) for r in other_poly.interiors]
    proj = Polygon(ext, holes).buffer(0)
    if proj.is_empty:
        return 0.0
    small = min(poly.area, proj.area) or 1e-12
    return poly.intersection(proj).area / small


def _existing_polys(space, cache, scale):
    """Existing boundaries of a space with their geometry as (record, frame, polygon)."""
    import numpy as np
    from shapely.geometry import Polygon
    out = []
    file = IfcStore.get_file()
    for r in _collect_boundaries(file, space, cache, scale):
        geo = r["_geo"]
        if not geo or not geo["loops"]:
            out.append((r, None, None))
            continue
        fr = _frame(geo["normal"], geo["loops"][0][0])
        try:
            poly = Polygon(_to2d(fr, geo["loops"][0]), [_to2d(fr, l) for l in geo["loops"][1:]]).buffer(0)
        except Exception:
            poly = None
        out.append((r, fr, poly))
    return out


def _host_elements(element):
    hosts = []
    for fv in getattr(element, "FillsVoids", []) or []:
        for ve in fv.RelatingOpeningElement.VoidsElements or []:
            hosts.append(ve.RelatingBuildingElement)
    return hosts


def _boundary_plan(file, spaces, search_depth=0.30, proximity=0.03, min_area=0.02,
                   include_coverings=True, max_thickness=1.0, candidate_classes=None):
    """Compute 2nd-level boundaries from geometry for the given spaces."""
    import numpy as np
    import ifcopenshell.util.unit as uu
    from shapely.ops import unary_union
    cache = _get_geometry_cache(False)
    tree = cache["tree"]
    scale = uu.calculate_unit_scale(file)
    classes = tuple(_as_list(candidate_classes)) or _BOUNDARY_CANDIDATE_CLASSES
    all_spaces = [s for s in file.by_type("IfcSpatialElement")
                  if (s.is_a("IfcSpace") or s.is_a("IfcExternalSpatialElement")) and s.id() in cache["shapes"]]
    geo_elements = [e for e in file.by_type("IfcGeographicElement") if e.id() in cache["shapes"]]
    candidates_all = [e for e in file.by_type("IfcElement")
                      if e.id() in cache["shapes"] and any(e.is_a(c) for c in classes)
                      and (include_coverings or not e.is_a("IfcCovering"))]
    plan = []
    for space in spaces:
        if space.id() not in cache["shapes"]:
            plan.append({"space": space, "error": "space has no 3D body geometry", "claims": [], "uncovered": [], "faces": []})
            continue
        near = {}
        for c in tree.clash_clearance_many([space], candidates_all, clearance=float(search_depth), check_all=True):
            other_id = c.b.id() if c.a.id() == space.id() else c.a.id()
            near[other_id] = file.by_id(other_id)  # never keep tree result instances
        for c in tree.clash_collision_many([space], candidates_all, allow_touching=False):
            other_id = c.b.id() if c.a.id() == space.id() else c.a.id()
            near[other_id] = file.by_id(other_id)  # never keep tree result instances
        faces = _space_faces(space, min_area)
        claims, uncovered = [], []
        for fi, face in enumerate(faces):
            fps = []
            for e in near.values():
                if face["position"] != "wall" and (e.is_a("IfcDoor") or e.is_a("IfcWindow")):
                    continue  # door/window faces lying on the floor plane are not floor boundaries
                try:
                    r = _footprint(face, e, search_depth, proximity, min_area)
                except Exception:
                    r = None
                if r:
                    fps.append((e, r[0], r[1], e.is_a("IfcCovering")))
            fps.sort(key=lambda x: x[2])
            remaining = face["poly"]
            face_claims = []
            for e, fp, dep, is_cov in fps:
                if remaining.area < min_area:
                    break
                parts = _parts(remaining.intersection(fp), min_area)
                for part in parts:
                    face_claims.append({"element": e, "poly": part, "depth": dep, "behind": None, "covering": is_cov})
                if parts:
                    remaining = remaining.difference(unary_union(parts))
            if include_coverings:
                for cl in [c for c in face_claims if c["covering"]]:
                    rem = cl["poly"]
                    for e, fp, dep, is_cov in fps:
                        if is_cov or dep <= cl["depth"] + 1e-4 or rem.area < min_area:
                            continue
                        parts = _parts(rem.intersection(fp), min_area)
                        for part in parts:
                            face_claims.append({"element": e, "poly": part, "depth": dep,
                                                "behind": cl["element"], "covering": False})
                        if parts:
                            rem = rem.difference(unary_union(parts))
            for cl in face_claims:
                cl["face"] = fi
                cl["frame"] = face["frame"]
                cl["position"] = face["position"]
                cl["space"] = space
                rp = cl["poly"].representative_point()
                p3 = _to3d(face["frame"], [[rp.x, rp.y]])[0]
                ie, s2, t = _classify_side(space, p3, face["frame"]["n"], all_spaces, geo_elements, max_thickness)
                cl.update({"internal_or_external": ie, "other_space": s2, "point": p3})
            # openings: parent boundary on the host
            for cl in face_claims:
                el = cl["element"]
                if el.is_a("IfcDoor") or el.is_a("IfcWindow"):
                    hosts = {h.id() for h in _host_elements(el)}
                    best, best_a = None, 0.0
                    for other in face_claims:
                        if other is cl or other["element"].id() not in hosts:
                            continue
                        a = other["poly"].buffer(0.05).intersection(cl["poly"]).area
                        if a > best_a:
                            best, best_a = other, a
                    cl["parent"] = best
                    cl["host_names"] = [file.by_id(h).Name for h in hosts]
            for part in _parts(remaining, max(min_area, 0.05)):
                rp = part.representative_point()
                p3 = _to3d(face["frame"], [[rp.x, rp.y]])[0]
                ie, s2, t = _classify_side(space, p3, face["frame"]["n"], all_spaces, geo_elements, max_thickness)
                uncovered.append({"face": fi, "frame": face["frame"], "poly": part, "position": face["position"],
                                  "internal_or_external": ie, "other_space": s2, "space": space})
            claims += face_claims
        # compare with existing boundaries
        existing = _existing_polys(space, cache, scale)
        used = set()
        for cl in claims:
            cl["matches"] = None
            for r, fr, poly in existing:
                el = r["_rel"].RelatedBuildingElement
                if poly is None or el is None or el.id() != cl["element"].id():
                    continue
                if abs(float(fr["n"] @ cl["frame"]["n"])) < 0.98:
                    continue
                if abs(float((fr["o"] - cl["frame"]["o"]) @ cl["frame"]["n"])) > search_depth + 0.01:
                    continue
                if _overlap_ratio(cl["frame"], cl["poly"], fr, poly) >= 0.5:
                    cl["matches"] = r["_rel"]
                    used.add(r["boundary_id"])
                    break
        plan.append({"space": space, "faces": faces, "claims": claims, "uncovered": uncovered,
                     "existing_unmatched": [r for r, fr, poly in existing if r["boundary_id"] not in used]})
    # corresponding boundaries between selected spaces
    index = {}
    for sp in plan:
        for cl in sp["claims"]:
            index.setdefault((cl["space"].id(), cl["element"].id()), []).append(cl)
    for sp in plan:
        for cl in sp["claims"]:
            if cl.get("corresponding") is not None or cl["other_space"] is None:
                continue
            best, best_r = None, 0.3
            for other in index.get((cl["other_space"].id(), cl["element"].id()), []):
                if other.get("corresponding") is not None:
                    continue
                if float(other["frame"]["n"] @ cl["frame"]["n"]) > -0.98:
                    continue
                sep = float((other["frame"]["o"] - cl["frame"]["o"]) @ cl["frame"]["n"])
                if sep <= 0 or sep > max_thickness:
                    continue
                r = _overlap_ratio(cl["frame"], cl["poly"], other["frame"], other["poly"])
                if r > best_r:
                    best, best_r = other, r
            if best is not None:
                cl["corresponding"], best["corresponding"] = best, cl
    return plan


def _claim_label(cl):
    el = cl["element"]
    return f"{el.Name or el.is_a()}"


def _claim_json(cl):
    out = {
        "element": _element_info(cl["element"]),
        "position": cl["position"],
        "area_m2": _r(cl["poly"].area, 3),
        "depth_m": _r(cl["depth"]),
        "internal_or_external": cl["internal_or_external"],
        "other_space": _space_label(cl["other_space"]) if cl["other_space"] else None,
        "kind": "2b" if cl.get("parent") else "2a",
        "status": f"exists (#{cl['matches'].id()})" if cl.get("matches") else "new",
    }
    if cl["behind"] is not None:
        out["behind_covering"] = cl["behind"].Name
    if cl["element"].is_a("IfcDoor") or cl["element"].is_a("IfcWindow"):
        out["parent_element"] = _claim_label(cl["parent"]) if cl.get("parent") else None
        if not cl.get("parent"):
            out["note"] = f"host boundary not found (host: {', '.join(n or '?' for n in cl.get('host_names', []))})"
    if cl.get("corresponding"):
        out["corresponding_in"] = _space_label(cl["corresponding"]["space"])
    return out


def _draw_poly(fr, poly, color, opacity, fill, label, group, meta):
    from mathutils import Vector
    from mathutils.geometry import tessellate_polygon
    rings = [list(poly.exterior.coords)[:-1]] + [list(r.coords)[:-1] for r in poly.interiors]
    loops = [_to3d(fr, r).tolist() for r in rings if len(r) >= 3]
    segs = []
    for loop in loops:
        segs += [(tuple(loop[i]), tuple(loop[(i + 1) % len(loop)])) for i in range(len(loop))]
    fills = None
    if fill:
        vecs = [[Vector(p) for p in loop] for loop in loops]
        flat = [v for loop in vecs for v in loop]
        tris = [[tuple(flat[i]) for i in t] for t in tessellate_polygon(vecs)]
        fills = [(tris, (color[0], color[1], color[2], opacity))]
    labels = []
    if label:
        rp = poly.representative_point()
        labels.append((label, tuple(_to3d(fr, [[rp.x, rp.y]])[0]), (1, 1, 1, 1)))
    return _add_item("boundary_suggestion", [(segs, color, 1.5)], labels, group, True, meta, fills=fills)


def suggest_space_boundaries(space_ids=None, search_depth=0.30, proximity=0.03, min_area=0.02,
                             include_coverings=True, max_thickness=1.0, candidate_classes=None,
                             draw=True, show_labels=False, opacity=0.35, rebuild_cache=False):
    """Preview 2nd-level space boundaries computed from geometry and compare with the existing ones."""
    try:
        import ifcopenshell.util.unit as uu
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        _get_geometry_cache(rebuild_cache)
        plan = _boundary_plan(file, spaces, search_depth, proximity, min_area, include_coverings,
                              max_thickness, candidate_classes)
        colors = {"new": (0.1, 0.85, 0.2, 1.0), "exists": (0.25, 0.55, 1.0, 1.0),
                  "behind": (0.85, 0.3, 0.85, 1.0), "opening": (0.0, 0.9, 0.95, 1.0),
                  "uncovered": (1.0, 0.9, 0.0, 1.0), "obsolete": (0.95, 0.1, 0.1, 1.0)}
        out, ids = [], []
        tot = {"proposed": 0, "new": 0, "exists": 0, "uncovered": 0, "existing_unmatched": 0}
        for sp in plan:
            space = sp["space"]
            claims = [_claim_json(c) for c in sp["claims"]]
            n_new = sum(1 for c in sp["claims"] if not c.get("matches"))
            tot["proposed"] += len(claims)
            tot["new"] += n_new
            tot["exists"] += len(claims) - n_new
            tot["uncovered"] += len(sp["uncovered"])
            tot["existing_unmatched"] += len(sp.get("existing_unmatched", []))
            out.append({
                "space": _space_label(space),
                "error": sp.get("error"),
                "faces": len(sp["faces"]),
                "proposed": len(claims),
                "new": n_new,
                "already_existing": len(claims) - n_new,
                "boundaries": claims,
                "uncovered_regions": [{"position": u["position"], "area_m2": _r(u["poly"].area, 3),
                                       "internal_or_external": u["internal_or_external"],
                                       "other_space": _space_label(u["other_space"]) if u["other_space"] else None}
                                      for u in sp["uncovered"]],
                "existing_not_reproduced": [{"boundary_id": r["boundary_id"],
                                             "element": (r["element"] or {}).get("name"),
                                             "position": r["position"], "area_m2": r["area_m2"]}
                                            for r in sp.get("existing_unmatched", [])],
            })
            if draw:
                group = f"suggest {_space_label(space)}"
                for c in sp["claims"]:
                    el = c["element"]
                    key = "exists" if c.get("matches") else ("opening" if (el.is_a("IfcDoor") or el.is_a("IfcWindow"))
                                                             else ("behind" if c["behind"] is not None else "new"))
                    lab = f"{_claim_label(c)} {c['poly'].area:.2f} m²" if show_labels else None
                    ids.append(_draw_poly(c["frame"], c["poly"], colors[key], opacity,
                                          c["behind"] is None, lab, group, {"space": _space_label(space), "status": key}))
                for u in sp["uncovered"]:
                    ids.append(_draw_poly(u["frame"], u["poly"], colors["uncovered"], opacity, True,
                                          f"virtual? {u['poly'].area:.2f} m²" if show_labels else None, group,
                                          {"space": _space_label(space), "status": "uncovered"}))
        result = {"units": "m", "summary": tot, "spaces": out}
        if draw:
            result["drawing_ids"] = ids
            result["legend"] = {"new": "green", "already exists": "blue", "behind covering": "magenta outline",
                                "door/window (2b)": "cyan", "uncovered (virtual candidate)": "yellow"}
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _remove_boundary_entity(file, rel):
    """Remove a boundary (and its Blender object if Bonsai loaded one), clearing references."""
    import ifcopenshell.api.boundary
    try:
        from bonsai import tool
        obj = tool.Ifc.get_object(rel)
        if obj is not None:
            bpy.data.objects.remove(obj)
    except Exception:
        pass
    for other in file.get_inverse(rel):
        for attr in ("ParentBoundary", "CorrespondingBoundary"):
            if getattr(other, attr, None) == rel:
                setattr(other, attr, None)
    try:
        ifcopenshell.api.boundary.remove_boundary(file, boundary=rel)
    except Exception:
        file.remove(rel)


def _new_boundary(file, space, element, cls, frame, poly, pv, ie, name, description, scale):
    import numpy as np
    import ifcopenshell.guid
    import ifcopenshell.api.boundary
    import ifcopenshell.util.placement as up
    from shapely.geometry.polygon import orient
    attrs = dict(GlobalId=ifcopenshell.guid.new(), Name=name, Description=description,
                 RelatingSpace=space, RelatedBuildingElement=element,
                 PhysicalOrVirtualBoundary=pv, InternalOrExternalBoundary=ie)
    if file.schema == "IFC2X3":
        histories = file.by_type("IfcOwnerHistory")
        attrs["OwnerHistory"] = histories[0] if histories else None
    rel = file.create_entity(cls, **attrs)
    ms = up.get_local_placement(space.ObjectPlacement) if getattr(space, "ObjectPlacement", None) else np.eye(4)
    inv = np.linalg.inv(ms)
    loc = (inv @ np.r_[frame["o"] / scale, 1.0])[:3] * scale
    rot = inv[:3, :3]
    axis = rot @ frame["n"]
    ref = rot @ frame["u"]
    poly = orient(poly, sign=1.0)
    outer = [tuple(c) for c in list(poly.exterior.coords)[:-1]]
    inner = [[tuple(c) for c in list(r.coords)[:-1]] for r in poly.interiors]
    kwargs = dict(rel_space_boundary=rel, outer_boundary=outer, inner_boundaries=inner,
                  location=tuple(loc), axis=tuple(axis / np.linalg.norm(axis)),
                  ref_direction=tuple(ref / np.linalg.norm(ref)), unit_scale=scale)
    ifcopenshell.api.boundary.assign_connection_geometry(file, **kwargs)
    return rel


def create_space_boundaries(space_ids=None, mode="add_missing", search_depth=0.30, proximity=0.03,
                            min_area=0.02, include_coverings=True, max_thickness=1.0,
                            candidate_classes=None, create_virtual=False, dry_run=False,
                            rebuild_cache=False):
    """Create 2nd-level IfcRelSpaceBoundary from geometry. mode: add_missing | replace."""
    try:
        import ifcopenshell.api
        import ifcopenshell.api.boundary
        import ifcopenshell.api.root
        import ifcopenshell.api.spatial
        import ifcopenshell.util.unit as uu
        import ifcopenshell.util.element as ue
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        if mode not in ("add_missing", "replace"):
            return {"error": "mode must be 'add_missing' or 'replace'"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        _get_geometry_cache(rebuild_cache)
        scale = uu.calculate_unit_scale(file)
        plan = _boundary_plan(file, spaces, search_depth, proximity, min_area, include_coverings,
                              max_thickness, candidate_classes)
        second = file.schema != "IFC2X3"
        cls = "IfcRelSpaceBoundary2ndLevel" if second else "IfcRelSpaceBoundary"
        to_create = [c for sp in plan for c in sp["claims"] if mode == "replace" or not c.get("matches")]
        to_remove = [r for sp in plan for r in (sp["space"].BoundedBy or [])] if mode == "replace" else []
        report = {
            "mode": mode, "dry_run": bool(dry_run), "class": cls,
            "to_remove": len(to_remove), "to_create": len(to_create),
            "virtual_to_create": sum(len(sp["uncovered"]) for sp in plan) if create_virtual else 0,
            "per_space": [{"space": _space_label(sp["space"]),
                           "create": sum(1 for c in sp["claims"] if mode == "replace" or not c.get("matches")),
                           "skip_existing": sum(1 for c in sp["claims"] if mode != "replace" and c.get("matches")),
                           "remove": len(sp["space"].BoundedBy or []) if mode == "replace" else 0}
                          for sp in plan],
        }
        if dry_run:
            return report
        for rel in to_remove:
            _remove_boundary_entity(file, rel)
        created = {}
        for c in to_create:
            desc = f"{_GEN_TAG}; {c['position']}" + (f"; behind {c['behind'].Name}" if c["behind"] is not None else "")
            rel = _new_boundary(file, c["space"], c["element"], cls, c["frame"], c["poly"], "PHYSICAL",
                                c["internal_or_external"], "2b" if c.get("parent") else "2a", desc, scale)
            created[id(c)] = rel
        linked_parent = linked_corr = 0
        if second:
            for c in to_create:
                rel = created[id(c)]
                parent = c.get("parent")
                prel = created.get(id(parent)) if parent else None
                if prel is None and parent is not None and parent.get("matches") is not None \
                        and parent["matches"].is_a("IfcRelSpaceBoundary2ndLevel"):
                    prel = parent["matches"]
                if prel is not None:
                    rel.ParentBoundary = prel
                    linked_parent += 1
                corr = c.get("corresponding")
                crel = created.get(id(corr)) if corr else None
                if crel is None and corr is not None and corr.get("matches") is not None \
                        and corr["matches"].is_a("IfcRelSpaceBoundary2ndLevel"):
                    crel = corr["matches"]
                # CorrespondingBoundary must be one-to-one and reciprocal (inverse Corresponds is SET [0:1])
                if crel is not None and rel.CorrespondingBoundary is None \
                        and crel.CorrespondingBoundary in (None, rel) \
                        and not [x for x in (getattr(crel, "Corresponds", None) or []) if x != rel] \
                        and not [x for x in (getattr(rel, "Corresponds", None) or []) if x != crel]:
                    rel.CorrespondingBoundary = crel
                    crel.CorrespondingBoundary = rel
                    linked_corr += 1
        virtual_created = 0
        if create_virtual:
            for sp in plan:
                container = ue.get_aggregate(sp["space"]) or ue.get_container(sp["space"])
                for u in sp["uncovered"]:
                    ve = ifcopenshell.api.root.create_entity(file, ifc_class="IfcVirtualElement",
                                                             name=f"Virtual boundary {_space_label(sp['space'])}")
                    if container is not None and container.is_a("IfcSpatialStructureElement"):
                        try:
                            ifcopenshell.api.spatial.assign_container(file, products=[ve], relating_structure=container)
                        except Exception:
                            pass
                    _new_boundary(file, sp["space"], ve, cls, u["frame"], u["poly"], "VIRTUAL",
                                  u["internal_or_external"], "2a", f"{_GEN_TAG}; {u['position']}; virtual", scale)
                    virtual_created += 1
        report.update({"removed": len(to_remove), "created": len(created), "virtual_created": virtual_created,
                       "parent_links": linked_parent, "corresponding_links": linked_corr,
                       "note": "Changes are in memory: save the IFC in Bonsai to keep them. "
                               "Undo with remove_space_boundaries(only_generated=True)."})
        return report
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def remove_space_boundaries(space_ids=None, boundary_ids=None, only_generated=True, dry_run=False):
    """Remove space boundaries (by default only those created by create_space_boundaries)."""
    try:
        import ifcopenshell.api.root
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        rels = []
        if boundary_ids:
            for i in _as_list(boundary_ids):
                try:
                    rels.append(file.by_id(int(str(i).lstrip("#"))))
                except Exception:
                    try:
                        rels.append(file.by_guid(str(i)))
                    except Exception:
                        pass
        else:
            for s in _resolve_spaces(file, space_ids):
                rels += list(s.BoundedBy or [])
        rels = [r for r in rels if r is not None and r.is_a("IfcRelSpaceBoundary")]
        if only_generated:
            rels = [r for r in rels if (r.Description or "").startswith(_GEN_TAG)]
        virtuals = {r.RelatedBuildingElement.id() for r in rels
                    if r.RelatedBuildingElement is not None and r.RelatedBuildingElement.is_a("IfcVirtualElement")
                    and (r.Description or "").startswith(_GEN_TAG)}
        result = {"dry_run": bool(dry_run), "boundaries": len(rels), "virtual_elements": len(virtuals),
                  "by_space": {}}
        for r in rels:
            k = _space_label(r.RelatingSpace)
            result["by_space"][k] = result["by_space"].get(k, 0) + 1
        if dry_run:
            return result
        for r in rels:
            _remove_boundary_entity(file, r)
        for vid in virtuals:
            try:
                ifcopenshell.api.root.remove_product(file, product=file.by_id(vid))
            except Exception:
                pass
        result["removed"] = len(rels)
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def fix_space_boundary_attributes(space_ids=None, overwrite=False, remove_duplicates=True,
                                  upgrade_to_second_level=False, link_openings=True, link_corresponding=True,
                                  max_thickness=1.0, dry_run=True, rebuild_cache=False):
    """Fill/repair attributes of existing boundaries from geometry (dry_run by default)."""
    try:
        import numpy as np
        import ifcopenshell.util.unit as uu
        from shapely.geometry import Polygon
        file = IfcStore.get_file()
        if file is None:
            return {"error": "No IFC file is currently loaded"}
        spaces = _resolve_spaces(file, space_ids)
        if not spaces:
            return {"error": "No spaces found"}
        cache = _get_geometry_cache(rebuild_cache)
        scale = uu.calculate_unit_scale(file)
        all_spaces = [s for s in file.by_type("IfcSpatialElement")
                      if (s.is_a("IfcSpace") or s.is_a("IfcExternalSpatialElement")) and s.id() in cache["shapes"]]
        geo_elements = [e for e in file.by_type("IfcGeographicElement") if e.id() in cache["shapes"]]
        changes = []

        def change(rel, attr, old, new, why):
            changes.append({"boundary_id": rel.id(), "space": _space_label(rel.RelatingSpace),
                            "element": (rel.RelatedBuildingElement.Name if rel.RelatedBuildingElement else None),
                            "attribute": attr, "old": old if not hasattr(old, "id") else f"#{old.id()}",
                            "new": new if not hasattr(new, "id") else f"#{new.id()}", "reason": why})

        # gather boundary geometry with outward normals
        info = {}
        for space in spaces:
            s_sum = cache["shapes"].get(space.id())
            center = np.mean([s_sum["aabb_min"], s_sum["aabb_max"]], axis=0) if s_sum else None
            for r in _collect_boundaries(file, space, cache, scale):
                geo = r["_geo"]
                if not geo or not geo["loops"]:
                    info[r["boundary_id"]] = {"rec": r, "frame": None, "poly": None}
                    continue
                n = np.asarray(geo["normal"])
                if center is not None and float((np.asarray(geo["centroid"]) - center) @ n) < 0:
                    n = -n
                fr = _frame(n, geo["loops"][0][0])
                try:
                    poly = Polygon(_to2d(fr, geo["loops"][0]), [_to2d(fr, l) for l in geo["loops"][1:]]).buffer(0)
                except Exception:
                    poly = None
                info[r["boundary_id"]] = {"rec": r, "frame": fr, "poly": poly}

        # 1. duplicates
        removed = set()
        if remove_duplicates:
            by_key = {}
            for bid, d in info.items():
                rel = d["rec"]["_rel"]
                el = rel.RelatedBuildingElement
                if el is None or d["poly"] is None:
                    continue
                by_key.setdefault((rel.RelatingSpace.id(), el.id()), []).append(bid)
            for key, ids in by_key.items():
                ids.sort()
                for i, a in enumerate(ids):
                    if a in removed:
                        continue
                    for b in ids[i + 1:]:
                        if b in removed:
                            continue
                        da, db = info[a], info[b]
                        if math.dist(da["rec"]["centroid"], db["rec"]["centroid"]) < 0.01 and \
                                abs(da["poly"].area - db["poly"].area) <= 0.01 * max(da["poly"].area, 1e-6):
                            removed.add(b)
                            change(db["rec"]["_rel"], "(removed)", None, None, f"duplicate of #{a}")

        # 2. upgrade class
        upgraded = {}
        if upgrade_to_second_level and file.schema != "IFC2X3":
            for bid, d in info.items():
                rel = d["rec"]["_rel"]
                if bid in removed or rel.is_a("IfcRelSpaceBoundary1stLevel") or rel.is_a("IfcRelSpaceBoundary2ndLevel"):
                    continue
                upgraded[bid] = True
                change(rel, "(class)", rel.is_a(), "IfcRelSpaceBoundary2ndLevel", "upgrade to 2nd level")

        # 3. PhysicalOrVirtual + InternalOrExternal
        side = {}
        for bid, d in info.items():
            if bid in removed:
                continue
            rel = d["rec"]["_rel"]
            el = rel.RelatedBuildingElement
            if rel.PhysicalOrVirtualBoundary in (None, "NOTDEFINED") or overwrite:
                pv = "VIRTUAL" if (el is not None and el.is_a("IfcVirtualElement")) else ("PHYSICAL" if el is not None else None)
                if pv and pv != rel.PhysicalOrVirtualBoundary:
                    change(rel, "PhysicalOrVirtualBoundary", rel.PhysicalOrVirtualBoundary, pv, "element type")
            if d["poly"] is None:
                continue
            rp = d["poly"].representative_point()
            p3 = _to3d(d["frame"], [[rp.x, rp.y]])[0]
            ie, s2, t = _classify_side(rel.RelatingSpace, p3, d["frame"]["n"], all_spaces, geo_elements, max_thickness)
            side[bid] = s2
            if (rel.InternalOrExternalBoundary in (None, "NOTDEFINED") or overwrite) and ie != rel.InternalOrExternalBoundary:
                why = f"ray hits {_space_label(s2)} at {t:.3f} m" if s2 else ("ray hits terrain" if ie == "EXTERNAL_EARTH" else "no space behind")
                change(rel, "InternalOrExternalBoundary", rel.InternalOrExternalBoundary, ie, why)

        def is_second(bid):
            rel = info[bid]["rec"]["_rel"]
            return rel.is_a("IfcRelSpaceBoundary2ndLevel") or bid in upgraded

        # 4. ParentBoundary for openings
        parent_of = {}
        if link_openings:
            for bid, d in info.items():
                rel = d["rec"]["_rel"]
                el = rel.RelatedBuildingElement
                if bid in removed or not is_second(bid) or el is None or not (el.is_a("IfcDoor") or el.is_a("IfcWindow")):
                    continue
                if getattr(rel, "ParentBoundary", None) is not None and not overwrite:
                    continue
                hosts = {h.id() for h in _host_elements(el)}
                best, best_a = None, 0.0
                for bid2, d2 in info.items():
                    r2 = d2["rec"]["_rel"]
                    if bid2 in removed or bid2 == bid or not is_second(bid2) or r2.RelatingSpace != rel.RelatingSpace:
                        continue
                    if r2.RelatedBuildingElement is None or r2.RelatedBuildingElement.id() not in hosts:
                        continue
                    if d2["poly"] is None or d["poly"] is None or abs(float(d2["frame"]["n"] @ d["frame"]["n"])) < 0.98:
                        continue
                    from shapely.geometry import Polygon as _P
                    ext = _to2d(d2["frame"], _to3d(d["frame"], list(d["poly"].exterior.coords)))
                    a = d2["poly"].buffer(0.05).intersection(_P(ext).buffer(0)).area
                    if a > best_a:
                        best, best_a = bid2, a
                if best is not None:
                    parent_of[bid] = best
                    change(rel, "ParentBoundary", getattr(rel, "ParentBoundary", None), f"#{best}", "host boundary")

        # 5. CorrespondingBoundary
        corr_of = {}
        if link_corresponding:
            for bid, d in info.items():
                rel = d["rec"]["_rel"]
                if bid in removed or not is_second(bid) or bid in corr_of or side.get(bid) is None or d["poly"] is None:
                    continue
                if getattr(rel, "CorrespondingBoundary", None) is not None and not overwrite:
                    continue
                el = rel.RelatedBuildingElement
                best, best_r = None, 0.3
                for bid2, d2 in info.items():
                    r2 = d2["rec"]["_rel"]
                    if bid2 in removed or bid2 in corr_of or not is_second(bid2) or d2["poly"] is None:
                        continue
                    if r2.RelatingSpace != side[bid] or el is None or r2.RelatedBuildingElement != el:
                        continue
                    if float(d2["frame"]["n"] @ d["frame"]["n"]) > -0.98:
                        continue
                    r = _overlap_ratio(d["frame"], d["poly"], d2["frame"], d2["poly"])
                    if r > best_r:
                        best, best_r = bid2, r
                if best is not None:
                    corr_of[bid], corr_of[best] = best, bid
                    change(rel, "CorrespondingBoundary", getattr(rel, "CorrespondingBoundary", None), f"#{best}",
                           f"same element in {_space_label(side[bid])} (overlap {best_r:.0%})")
                    prel = info[best]["rec"]["_rel"]
                    change(prel, "CorrespondingBoundary", getattr(prel, "CorrespondingBoundary", None), f"#{bid}",
                           f"reciprocal of #{bid}")

        summary = {}
        for c in changes:
            summary[c["attribute"]] = summary.get(c["attribute"], 0) + 1
        result = {"dry_run": bool(dry_run), "boundaries_checked": len(info), "change_count": len(changes),
                  "by_attribute": summary, "changes": changes}
        if not upgrade_to_second_level and file.schema != "IFC2X3" and (link_openings or link_corresponding):
            base = sum(1 for bid in info if not is_second(bid))
            if base:
                result["note"] = (f"{base} boundaries are plain IfcRelSpaceBoundary: ParentBoundary/"
                                  "CorrespondingBoundary only exist on IfcRelSpaceBoundary2ndLevel. "
                                  "Use upgrade_to_second_level=True to convert them.")
        if dry_run:
            return result

        # apply
        new_rel = {}
        for bid in removed:
            _remove_boundary_entity(file, info[bid]["rec"]["_rel"])
        for bid in upgraded:
            old = info[bid]["rec"]["_rel"]
            attrs = old.get_info(recursive=False)
            for k in ("id", "type"):
                attrs.pop(k, None)
            try:
                from bonsai import tool
                obj = tool.Ifc.get_object(old)
                if obj is not None:
                    bpy.data.objects.remove(obj)
            except Exception:
                pass
            file.remove(old)
            new_rel[bid] = file.create_entity("IfcRelSpaceBoundary2ndLevel", **attrs)

        def rel_of(bid):
            return new_rel.get(bid) or info[bid]["rec"]["_rel"]

        for c in changes:
            bid = c["boundary_id"]
            if bid in removed or c["attribute"].startswith("("):
                continue
            rel = rel_of(bid)
            if c["attribute"] == "ParentBoundary":
                rel.ParentBoundary = rel_of(parent_of[bid])
            elif c["attribute"] == "CorrespondingBoundary":
                rel.CorrespondingBoundary = rel_of(corr_of[bid])
            else:
                setattr(rel, c["attribute"], c["new"])
        result["applied"] = True
        result["note_save"] = "Changes are in memory: save the IFC in Bonsai to keep them."
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Bonsai MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Bonsai MCP'
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        layout.prop(scene, "blendermcp_port")
        
        if not scene.blendermcp_server_running:
            layout.operator("blendermcp.start_server", text="Start MCP Server")
        else:
            layout.operator("blendermcp.stop_server", text="Stop MCP Server")
            layout.label(text=f"Running on port {scene.blendermcp_port}")


# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the BlenderMCP server to connect with Claude"
    
    def execute(self, context):
        scene = context.scene
        
        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)
        
        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = True
        
        return {'FINISHED'}

# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"
    
    def execute(self, context):
        scene = context.scene
        
        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server
        
        scene.blendermcp_server_running = False
        
        return {'FINISHED'}

# Registration functions
def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535
    )
    
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running",
        default=False
    )
    
    
    _remove_overlay_handlers()  # drop drawings left by a previous load of this add-on
    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)
    
    print("BlenderMCP addon registered")

def unregister():
    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server
    
    _OVERLAY["items"] = []
    _remove_overlay_handlers()
    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    
    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running

    print("BlenderMCP addon unregistered")

if __name__ == "__main__":
    register()
