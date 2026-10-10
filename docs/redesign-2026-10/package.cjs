const fs=require('fs');const path=require('path');
const read=p=>fs.readFileSync(path.join(__dirname,p),'utf8');
const html=read('prototype.html').replace('<link rel="stylesheet" href="prototype.css">','<style>'+read('prototype.css')+'</style>').replace('<script src="prototype.js"></script>','<script>'+read('prototype.js')+'</script>');
fs.writeFileSync(path.join(__dirname,'LevelFrame-mockup.html'),html);
console.log('Standalone mockup written');
