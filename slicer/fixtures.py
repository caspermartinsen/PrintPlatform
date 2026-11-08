"""Small deterministic 3MF projects for parser and real-slicer verification."""
import io
import json
import zipfile


def project_file(colors=('#FF0000','#0000FF'), painted=False, layered=False):
    vertices = [(0,0,0),(8,0,0),(8,8,0),(0,8,0),(0,0,1),(8,0,1),(8,8,1),(0,8,1)]
    triangles = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
                 (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    meshes, objects, instances, build = [], [], [], []
    for index in range(len(colors)):
        item = index + 1
        assigned = 1 if layered else item
        mesh = '<object id="' + str(item) + '" type="model"><mesh><vertices>'
        mesh += ''.join('<vertex x="%s" y="%s" z="%s"/>' % vertex for vertex in vertices)
        mesh += '</vertices><triangles>' + ''.join('<triangle v1="%s" v2="%s" v3="%s"%s/>' % (*triangle,' paint_color="4"' if painted else '') for triangle in triangles)
        meshes.append(mesh + '</triangles></mesh></object>')
        objects.append('<object id="%s"><metadata key="name" value="Color %s"/><metadata key="extruder" value="%s"/><part id="%s" subtype="normal_part"><metadata key="extruder" value="%s"/></part></object>' % (item,item,assigned,item,assigned))
        instances.append('<model_instance><metadata key="object_id" value="%s"/><metadata key="instance_id" value="0"/><metadata key="identify_id" value="%s"/></model_instance>' % (item,item))
        build.append('<item objectid="%s" transform="1 0 0 0 1 0 0 0 1 %s 100 0"/>' % (item,100 + index * 20))
    model = '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><metadata name="Application">BambuStudio</metadata><metadata name="BambuStudio:3mfVersion">1</metadata><resources>' + ''.join(meshes) + '</resources><build>' + ''.join(build) + '</build></model>'
    settings = {'printer_model':'Bambu Lab P2S','filament_colour':list(colors),'filament_type':['PLA'] * len(colors),
                'filament_settings_id':['Bambu PLA Basic @BBL P2S'] * len(colors),'filament_diameter':['1.75'] * len(colors)}
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        archive.writestr('3D/3dmodel.model',model)
        archive.writestr('Metadata/project_settings.config',json.dumps(settings))
        archive.writestr('Metadata/model_settings.config','<config>' + ''.join(objects) + '<plate><metadata key="plater_id" value="1"/>' + ''.join(instances) + '</plate></config>')
        if layered:
            archive.writestr('Metadata/custom_gcode_per_layer.xml','<custom_gcodes_per_layer><plate><plate_info id="1"/><layer top_z="0.6" type="2" extruder="2" color="#0000FF" extra="" gcode="tool_change"/><mode value="MultiAsSingle"/></plate></custom_gcodes_per_layer>')
    return output.getvalue()


def sliced_file(ids=(1,2), colors=('#FF0000','#0000FF')):
    count = max(ids)
    settings = {'printer_model':'Bambu Lab P2S','curr_bed_type':'Textured PEI Plate','layer_height':'0.2',
                'sparse_infill_density':'15%','filament_settings_id':['Bambu PLA Basic @BBL P2S'] * count}
    filaments = ''.join('<filament id="%s" type="PLA" color="%s" used_g="1"/>' % (item,colors[index]) for index,item in enumerate(ids))
    gcode = 'G90\nM83\nG1 X100 Y100 Z0.2\n; CHANGE_LAYER\n'
    for index,item in enumerate(ids):
        gcode += 'T%s\nG1 X%s Y100 E1\nT255\nG1 X%s Y110 E1\n' % (item-1,110 + index*10,110 + index*10)
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('Metadata/project_settings.config',json.dumps(settings))
        archive.writestr('Metadata/slice_info.config','<config><plate><metadata key="prediction" value="600"/><metadata key="printer_model_id" value="N7"/><metadata key="nozzle_diameters" value="0.4"/>' + filaments + '</plate></config>')
        archive.writestr('Metadata/plate_1.gcode',gcode)
    return output.getvalue()


def standard_project_file():
    import xml.etree.ElementTree as ET
    safe_xml = ET.fromstring
    ns={'m':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(project_file())) as source, zipfile.ZipFile(output,'w') as target:
        root=safe_xml(source.read('3D/3dmodel.model'))
        resources=root.find('m:resources',ns)
        palette=ET.SubElement(resources,'{'+ns['m']+'}basematerials',id='10')
        for color in ('#151515FF','#2879E8FF'):
            ET.SubElement(palette,'{'+ns['m']+'}base',name='Color',displaycolor=color)
        for index,item in enumerate(resources.findall('m:object',ns)):
            item.set('pid','10');item.set('pindex',str(index))
            for vertex in item.findall('m:mesh/m:vertices/m:vertex',ns):
                vertex.set('x',str(float(vertex.get('x'))-4))
                vertex.set('y',str(float(vertex.get('y'))-4))
        assembly=ET.SubElement(resources,'{'+ns['m']+'}object',id='3',type='model')
        components=ET.SubElement(assembly,'{'+ns['m']+'}components')
        for object_id in ('1','2'):
            ET.SubElement(components,'{'+ns['m']+'}component',objectid=object_id,transform='1 0 0 0 1 0 0 0 1 '+('12' if object_id=='2' else '0')+' 0 0')
        build=root.find('m:build',ns);build.clear()
        ET.SubElement(build,'{'+ns['m']+'}item',objectid='3')
        for name in source.namelist():
            if name.startswith('Metadata/'):continue
            target.writestr(name,ET.tostring(root) if name=='3D/3dmodel.model' else source.read(name))
    return output.getvalue()
