
function Tabs(){
    let tabs = ["About", "Projects", "Experince"]
    return (
        <>
            <ul className="nav justify-content-center">
                {tabs.map((tabname)=>(
                     <li className="nav-item" key={tabname}><a className="nav-link" href={tabname}>{tabname}</a></li>
                ))}
            </ul>
        </>
    )
}

export default Tabs